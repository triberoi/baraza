"""Luma → the store. Two ways in, one set of rows out.

:func:`import_folder` is the free path: any Luma user can export a per-event guest list
without an API key. :func:`import_luma_api` is the paid path, and simpler, because the
API answers with the event's name and dates.

Both derive a guest's status through the same mapping, so a no-show cannot depend on
which route imported it.

An event's name and date come from the store first (a resolved event and one the
organizer typed look alike, deliberately — their typed date must not be overwritten, and
a re-import then makes no network calls), then the metadata resolver, then nothing, in
which case the event is pending rather than an error.

Import is idempotent: every write underneath is an upsert on Luma's own grain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from baraza.ingest import (
    LumaClient,
    LumaGuest,
    LumaGuestRow,
    LumaParseError,
    ParsedLumaFile,
    ResolveFn,
    combine_luma_guest_lists,
    email_to_member_id,
    parse_luma_guest_list,
    resolve_guest_status,
    resolve_luma_event,
    resolve_with_retry,
)
from baraza.store import Store, StoredAttendance, StoredEvent, StoredMember


@dataclass(frozen=True)
class PendingEvent:
    """An event whose guests were read but whose name and date are still unknown.

    Carries the guest count and filenames because ``evt-mBc2X…`` identifies nothing to a
    human and the inferred title is often absent.
    """

    luma_event_id: str
    inferred_title: str | None
    guests: int
    sources: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ImportResult:
    """What one import pass did, from a folder or from the API.

    The counts are rows this pass WROTE, and every write is an upsert, so they are
    neither additions nor changes: re-importing an unchanged folder reports the same
    numbers and moves nothing. A count of 0 means this pass had nothing to say about that
    table, never that the store is empty.

    ``files_read``, ``files_skipped`` and ``pending`` are folder-only.
    """

    files_read: int = 0
    files_skipped: tuple[str, ...] = ()
    events: int = 0
    members: int = 0
    attendances: int = 0
    pending: tuple[PendingEvent, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)


def read_folder(folder: Path | str) -> tuple[list[ParsedLumaFile], list[str], list[str]]:
    """Parse every ``*.csv`` in ``folder``: ``(parsed, skipped, warnings)``.

    Not recursive — a downloads folder is flat, and walking the tree turns "point at your
    downloads" into a scan of everything the organizer has ever saved. A file that is not
    a Luma export is skipped, not fatal.

    The extension match is case-insensitive because ``glob("*.csv")`` is case-sensitive
    on macOS and Linux only, and a file the glob never matched never reaches
    ``files_skipped`` either — so the organizer sees "0 events" and no reason.
    """
    directory = Path(folder)
    if not directory.is_dir():
        raise NotADirectoryError(f"{directory} is not a folder")

    parsed: list[ParsedLumaFile] = []
    skipped: list[str] = []
    warnings: list[str] = []
    csv_files = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".csv"),
        key=lambda p: p.name,
    )
    for path in csv_files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(path.name)
            warnings.append(f"{path.name}: could not be read ({exc.__class__.__name__}) — skipped.")
            continue
        try:
            parsed.append(ParsedLumaFile(path.name, parse_luma_guest_list(text)))
        except LumaParseError as exc:
            skipped.append(path.name)
            warnings.append(f"{path.name}: {exc}")
    return parsed, skipped, warnings


def _as_luma_guest(row: LumaGuestRow) -> LumaGuest:
    """The guest-list CSV's row in the shape the shared status mapping takes, so the
    free path and the API path derive a no-show the same way rather than twice."""
    return LumaGuest(
        api_id=row.luma_guest_id,
        approval_status=row.luma_approval_status,
        user_email=row.email,
        user_name=row.name,
        registered_at=row.rsvp_at,
        checked_in_at=row.checked_in_at,
    )


def normalize_email(raw: str | None) -> str:
    """Trim and lowercase, treating whitespace-only as empty.

    The empty case is the point: a single space is truthy, so ``if not email`` lets it
    through, and hashing the stripped result collapses every such guest onto one member
    id — one fictional person, scored and counted like anyone else.

    Validated here rather than inside ``email_to_member_id``: that hash is published, its
    outputs are persisted and joined on, and making it raise would be a breaking change.
    """
    return (raw or "").strip().lower()


def _attendance_supersedes(new: StoredAttendance, existing: StoredAttendance) -> bool:
    """Which of two registrations by the same person for the same event survives.

    One person can hold two Luma guest ids for one event — registered, cancelled,
    registered again. The combiner keeps both (they are genuinely two guest records) and
    they collide here, where attendances are keyed on ``(event_id, member_id)``.

    Decide on evidence, never arrival order: attendance wins, then a check-in timestamp,
    then the later RSVP, then ``status`` alphabetically so the order is total. Without
    that, renaming an export flips someone between ``attended`` and ``no_show``.
    """
    if (new.status == "attended") != (existing.status == "attended"):
        return new.status == "attended"
    if (new.checked_in_at is not None) != (existing.checked_in_at is not None):
        return new.checked_in_at is not None
    if new.checked_in_at is not None and existing.checked_in_at is not None:
        if new.checked_in_at != existing.checked_in_at:
            return new.checked_in_at > existing.checked_in_at
    if new.rsvp_at != existing.rsvp_at:
        if new.rsvp_at is None:
            return False
        if existing.rsvp_at is None:
            return True
        return new.rsvp_at > existing.rsvp_at
    return new.status > existing.status


def _collapse_attendances(records: list[StoredAttendance]) -> list[StoredAttendance]:
    """Collapse to one record per ``(event_id, member_id)`` — the store's own key —
    deciding by :func:`_attendance_supersedes` rather than by who was written last.

    Done HERE rather than left to the store's ``ON CONFLICT``, because the store sees
    two rows arriving and has no basis to prefer either; the importer knows they are
    the same person's two registrations for one event, which is what makes the
    evidence rule meaningful. The output is sorted so the written order is a function
    of the data and not of the folder.
    """
    best: dict[tuple[str, str], StoredAttendance] = {}
    for record in records:
        key = (record.event_id, record.member_id)
        current = best.get(key)
        if current is None or _attendance_supersedes(record, current):
            best[key] = record
    return [best[key] for key in sorted(best)]


def import_folder(
    store: Store,
    folder: Path | str,
    *,
    now: datetime,
    resolve: ResolveFn | None = resolve_luma_event,
) -> ImportResult:
    """Read ``folder`` into ``store``. See the module docstring for where each event's
    name and date come from.

    ``resolve`` is injectable and may be ``None`` to work entirely offline — the tests
    bind a fake, and an organizer on a plane still gets their pending list.
    """
    parsed, skipped, warnings = read_folder(folder)
    combined = combine_luma_guest_lists(parsed)
    warnings.extend(w for event in combined.events for w in event.warnings)

    known = {event.event_id: event for event in store.events()}

    events: list[StoredEvent] = []
    members: dict[str, StoredMember] = {}
    attendances: list[StoredAttendance] = []
    pending: list[PendingEvent] = []

    for event in sorted(combined.events, key=lambda e: e.luma_event_id):
        stored = known.get(event.luma_event_id)
        if stored is not None:
            title, starts_at, ends_at = stored.title, stored.starts_at, stored.ends_at
        else:
            found = resolve_with_retry(event.luma_event_id, resolve) if resolve is not None else None
            if found is None or found.kind != "resolved":
                pending.append(
                    PendingEvent(
                        luma_event_id=event.luma_event_id,
                        inferred_title=event.inferred_title,
                        guests=len(event.guests),
                        sources=tuple(event.sources),
                        reason="no_resolver" if found is None else found.reason,
                    )
                )
                continue
            title, starts_at, ends_at = found.name, found.start_at, found.end_at

        events.append(StoredEvent(event.luma_event_id, title, starts_at, ends_at))
        for guest in event.guests:
            email = normalize_email(guest.email)
            if not email:
                # No email means no member id, so the guest cannot be joined across
                # exports. Warned rather than dropped in silence.
                warnings.append(f"{event.luma_event_id}: a guest with no email address was not imported.")
                continue
            member_id = email_to_member_id(email)
            members.setdefault(member_id, StoredMember(member_id, email, (guest.name or '').strip()))
            # The cut-off is the event's END when one is known, its start when not.
            # The same expression runs on both routes, which is what stops a derived
            # no-show depending on which route imported it. Do not "fix" the agreement
            # into a divergence: a CSV usually has no end, but that is the data
            # differing, not the rule.
            status = resolve_guest_status(_as_luma_guest(guest), event_ended_at=ends_at or starts_at, now=now)
            attendances.append(
                StoredAttendance(
                    event_id=event.luma_event_id,
                    member_id=member_id,
                    status=status,
                    rsvp_at=guest.rsvp_at,
                    checked_in_at=guest.checked_in_at,
                    luma_approval_status=guest.luma_approval_status,
                )
            )

    collapsed = _collapse_attendances(attendances)
    store.write_import(events, list(members.values()), collapsed)

    # Persist what this pass could not name, merged over what earlier passes could not.
    # Before this, the pending list lived only in this function's RETURN VALUE, so the
    # Import screen could render it exactly once — reload the page, or arrive from the
    # CLI's "name them in the app", and the list was gone with no way back but
    # re-typing the folder path. The store filters out anything since named, so this
    # pass's answer wins per event id and a named event retires itself.
    merged = {entry.get("luma_event_id"): entry for entry in store.pending_events()}
    for unnamed in pending:
        merged[unnamed.luma_event_id] = {
            "luma_event_id": unnamed.luma_event_id,
            "inferred_title": unnamed.inferred_title,
            "guests": unnamed.guests,
            "sources": list(unnamed.sources),
            "reason": unnamed.reason,
        }
    store.set_pending_events(list(merged.values()))
    store.set_last_import_folder(str(Path(folder).resolve()))

    return ImportResult(
        files_read=len(parsed),
        files_skipped=tuple(skipped),
        events=len(events),
        members=len(members),
        attendances=len(collapsed),
        pending=tuple(pending),
        warnings=tuple(warnings),
    )


def import_luma_api(store: Store, client: LumaClient, *, now: datetime) -> ImportResult:
    """Read the whole calendar straight from Luma into ``store``.

    The paid path, and simpler: the API answers with name, start, end, venue and tags, so
    there is no resolver and nothing can end up pending.

    Luma wins on a conflict here, the opposite of the folder path, because the calendar
    itself is answering and whatever the store holds arrived by some earlier route.

    A blank from Luma is not an answer and overwrites nothing — that rule lives in
    :meth:`Store.upsert_events`, so it holds for every caller. Guests with no email are
    skipped and counted, as on the folder path; see :func:`normalize_email`.
    """
    events: list[StoredEvent] = []
    members: dict[str, StoredMember] = {}
    attendances: list[StoredAttendance] = []
    warnings: list[str] = []

    for event in sorted(client.list_events(), key=lambda e: e.api_id):
        events.append(
            StoredEvent(
                event_id=event.api_id,
                title=event.name,
                starts_at=event.start_at,
                ends_at=event.end_at,
                venue=event.venue,
                tags=event.tags,
            )
        )
        for guest in client.list_event_guests(event.api_id):
            email = normalize_email(guest.user_email)
            if not email:
                warnings.append(f"{event.api_id}: a guest with no email address was not imported.")
                continue
            member_id = email_to_member_id(email)
            members.setdefault(member_id, StoredMember(member_id, email, (guest.user_name or '').strip()))
            attendances.append(
                StoredAttendance(
                    event_id=event.api_id,
                    member_id=member_id,
                    # The SAME expression the folder route uses. The API usually
                    # knows an end, so the fallback rarely runs here — a difference in
                    # the data, not the rule.
                    status=resolve_guest_status(guest, event_ended_at=event.end_at or event.start_at, now=now),
                    rsvp_at=guest.registered_at,
                    checked_in_at=guest.checked_in_at,
                    luma_approval_status=guest.approval_status,
                )
            )

    collapsed = _collapse_attendances(attendances)
    store.write_import(events, list(members.values()), collapsed)

    return ImportResult(
        events=len(events),
        members=len(members),
        attendances=len(collapsed),
        warnings=tuple(warnings),
    )


__all__ = [
    "ImportResult",
    "PendingEvent",
    "import_folder",
    "import_luma_api",
    "normalize_email",
    "read_folder",
]
