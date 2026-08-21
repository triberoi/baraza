"""Reading a Luma guest-list CSV export: parse one file, combine many. The free path —
any Luma user can export these without an API key.

Both stages are pure. A per-row anomaly warns and skips the row; only a file with no
``evt-`` id raises :class:`LumaParseError`. A guest-list CSV carries no event name or
start time, so the caller supplies those.
"""

from __future__ import annotations

import csv
import io
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from baraza.ingest.mappers import KNOWN_APPROVAL_STATUSES

_EVT_ID_RE = re.compile(r"evt-[A-Za-z0-9]+")
# Luma names the download with spaces (`AI Night - Guests - <ts>.csv`); underscores
# appear when something along the way (a browser, a sync client) sanitizes the name.
# The original pattern knew only the underscore form, so every title in an unrenamed
# real download came back as "(no title in the filename)" and the organizer retyped
# names the filenames already carried.
_FILENAME_TITLE_RE = re.compile(
    r"^(.+?)[ _]-[ _]Guests[ _]-[ _]\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.csv$", re.IGNORECASE
)

# Feedback is member free text; cap what one CSV cell can push into a row.
FEEDBACK_MAX_CHARS = 2000

#: The columns without which this parser cannot work. Deliberately not every column
#: Luma emits — the survey pair, ``checked_in_at`` and ``name`` all come and go between
#: exports, and refusing a file over an optional column loses a good import.
_REQUIRED_COLUMNS = frozenset({"guest_id", "email", "created_at", "approval_status", "qr_code_url"})

# `csv` raises on an oversized field before yielding any row, so one big cell would
# cost the whole file. The text is already in memory, so raising the ceiling to its own
# length admits nothing new. `field_size_limit` takes a C long, 32-bit on Windows.
_FIELD_LIMIT_MAX = 2**31 - 1


class LumaParseError(Exception):
    """A file-level problem — not a Luma guest-list export (no ``evt-`` id)."""


@dataclass(frozen=True)
class LumaGuestRow:
    luma_guest_id: str
    name: str
    email: str  # lowercased, trimmed; the join key
    rsvp_at: datetime
    # Luma's own word, carried through verbatim. Rewriting an unrecognized one destroys
    # the only evidence of what Luma said and resolves to the wrong status.
    luma_approval_status: str
    checked_in_at: datetime | None
    # Luma's post-event feedback: a 1-5 star rating + optional free text.
    survey_response_rating: int | None = None
    survey_response_feedback: str | None = None


@dataclass(frozen=True)
class LumaEventGuestList:
    luma_event_id: str
    guests: list[LumaGuestRow]
    warnings: list[str]


def _parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_rating(raw: str, idx: int, warnings: list[str]) -> int | None:
    """A 1-5 star integer; exports sometimes render it as ``4.0``. Anything else warns and
    returns None rather than blocking the guest row."""
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        warnings.append(f"row {idx}: survey_response_rating {raw!r} is not a number — rating dropped.")
        return None
    if not value.is_integer() or not (1 <= value <= 5):
        warnings.append(f"row {idx}: survey_response_rating {raw!r} is not a whole number 1-5 — rating dropped.")
        return None
    return int(value)


def _resolve_name(row: dict[str, str]) -> str:
    """What this export calls the guest, or ``""`` when it does not name them.

    Never invents a stand-in: an invented name is indistinguishable from a real one, and
    the store's rule that a blank never overwrites a known name needs the blank.
    :attr:`baraza.store.StoredMember.display_name` derives a stand-in for display.
    """
    name = (row.get("name") or "").strip()
    if name:
        return name
    first, last = (row.get("first_name") or "").strip(), (row.get("last_name") or "").strip()
    return f"{first} {last}".strip()


#: Serializes the field-limit window. See :func:`_relaxed_field_limit`.
_FIELD_LIMIT_LOCK = threading.Lock()


@contextmanager
def _relaxed_field_limit(text_length: int) -> Iterator[None]:
    """Raise `csv`'s module-global field ceiling for one parse, then put it back.

    KEEP THE LOCK. The ceiling is process-global and this is a library: without it two
    concurrent parses read each other's raised value as "previous" and leave the limit
    raised for the whole process, host application included. The lock spans the parse,
    not just the assignments, because the ceiling must hold while the reader runs.

    Residual: a host parsing CSV on another thread during our parse still sees the raised
    ceiling. The stdlib offers no way to avoid the global.
    """
    with _FIELD_LIMIT_LOCK:
        previous = csv.field_size_limit()
        csv.field_size_limit(max(previous, min(text_length + 1, _FIELD_LIMIT_MAX)))
        try:
            yield
        finally:
            csv.field_size_limit(previous)


def parse_luma_guest_list(csv_text: str) -> LumaEventGuestList:
    """Parse one Luma per-event guest-list CSV. Pure. Raises ``LumaParseError`` only
    when the file is not readable as a Luma export — no ``evt-`` id anywhere, or CSV
    the reader cannot tokenize at all."""
    stripped = csv_text.lstrip("﻿")
    warnings: list[str] = []
    try:
        with _relaxed_field_limit(len(stripped)):
            reader = csv.DictReader(io.StringIO(stripped))
            fieldnames = list(reader.fieldnames or ())
            rows = list(reader)
    except csv.Error as exc:
        # Malformed beyond the reader: an unterminated quote, a NUL byte. One message
        # class, which the caller's fall-through to native-CSV detection is keyed on.
        raise LumaParseError(f"This file could not be read as CSV ({exc.__class__.__name__}).") from exc

    # Header problems are settled once here rather than re-discovered per row.
    duplicated = sorted({name for name in fieldnames if fieldnames.count(name) > 1})
    for name in duplicated:
        # `DictReader` keeps the LAST of a repeated column silently. For `email` that
        # re-identifies the guest, since the address is the member id.
        warnings.append(f"the column {name!r} appears more than once — only the last one was read.")

    missing = sorted(_REQUIRED_COLUMNS.difference(fieldnames))
    if missing:
        # Fail on the header, not per row: a renamed column otherwise drops every row
        # with a warning that misdescribes the cause, once per guest.
        raise LumaParseError(
            "This file is missing "
            + ("a column" if len(missing) == 1 else "columns")
            + " a Luma guest-list export has: "
            + ", ".join(repr(name) for name in missing)
            + "."
        )

    luma_event_id: str | None = None
    for row in rows:
        m = _EVT_ID_RE.search((row.get("qr_code_url") or "").strip())
        if m:
            luma_event_id = m.group(0)
            break
    if luma_event_id is None:
        raise LumaParseError(
            "This file does not appear to be a Luma guest-list export — no 'evt-' id found in the qr_code_url column."
        )

    guests: list[LumaGuestRow] = []
    for idx, row in enumerate(rows, start=1):
        guest_id = (row.get("guest_id") or "").strip()
        if not guest_id:
            warnings.append(f"row {idx}: missing guest_id — skipped.")
            continue
        email = (row.get("email") or "").strip().lower()
        if not email:
            # State what THIS layer did and no more: the importer downstream reports its
            # own outcome into the same list, and the two must not contradict.
            warnings.append(f"row {idx}: no email address — this guest cannot be joined across exports.")
        rsvp_at = _parse_date((row.get("created_at") or "").strip())
        if rsvp_at is None:
            warnings.append(f"row {idx}: created_at is not a parseable date — row skipped.")
            continue
        approval = (row.get("approval_status") or "").strip()
        if approval not in KNOWN_APPROVAL_STATUSES:
            warnings.append(f"row {idx}: approval_status {approval!r} is not one we recognize — carried through as-is.")
        checked_raw = (row.get("checked_in_at") or "").strip()
        checked_in_at = _parse_date(checked_raw) if checked_raw else None
        if checked_raw and checked_in_at is None:
            warnings.append(f"row {idx}: checked_in_at not parseable — treating as not checked in.")
        rating = _parse_rating((row.get("survey_response_rating") or "").strip(), idx, warnings)
        feedback = (row.get("survey_response_feedback") or "").strip() or None
        if feedback and len(feedback) > FEEDBACK_MAX_CHARS:
            warnings.append(f"row {idx}: survey_response_feedback truncated to {FEEDBACK_MAX_CHARS} characters.")
            feedback = feedback[:FEEDBACK_MAX_CHARS]
        guests.append(
            LumaGuestRow(
                guest_id, _resolve_name(row), email, rsvp_at, approval, checked_in_at, rating, feedback
            )
        )

    return LumaEventGuestList(luma_event_id, guests, warnings)


# --- combine ------------------------------------------------------------------
@dataclass(frozen=True)
class ParsedLumaFile:
    filename: str
    guest_list: LumaEventGuestList


@dataclass(frozen=True)
class ResolvedEvent:
    luma_event_id: str
    sources: list[str]
    inferred_title: str | None
    guests: list[LumaGuestRow]
    warnings: list[str]


@dataclass(frozen=True)
class CombinerResult:
    events: list[ResolvedEvent]
    deduplicated_guest_count: int
    unique_member_count: int


def infer_title_from_filename(filename: str) -> str | None:
    """Title from the Luma export convention ``Name_-_Guests_-_<ts>.csv``."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    m = _FILENAME_TITLE_RE.match(base)
    return (m.group(1).replace("_", " ").strip() or None) if m else None


def _supersedes(new: LumaGuestRow, existing: LumaGuestRow) -> bool:
    """Which of two rows for the same guest survives deduplication.

    Order of evidence: a check-in wins outright (Luma never un-checks a guest), then the
    later RSVP, then a row that names the guest. When all of that ties, the later-processed
    row wins — and :func:`combine_luma_guest_lists` processes files in filename order, which
    for Luma's ``Name_-_Guests_-_<ts>.csv`` convention is export-timestamp order, so the tie
    resolves to the more recent export. That order is established by the caller-independent
    sort in the combiner; without it this resolves to whichever file the caller passed first.
    """
    new_checked, existing_checked = new.checked_in_at is not None, existing.checked_in_at is not None
    if new_checked != existing_checked:
        return new_checked
    if new.rsvp_at != existing.rsvp_at:
        return new.rsvp_at > existing.rsvp_at
    new_named, existing_named = bool(new.name.strip()), bool(existing.name.strip())
    if new_named != existing_named:
        return new_named
    return True


def combine_luma_guest_lists(files: list[ParsedLumaFile]) -> CombinerResult:
    """Group parsed files by event, dedupe guests (see :func:`_supersedes`), aggregate.
    Pure (no API metadata resolution — event name/start is caller-supplied).

    Files are processed in filename order, not the order the caller supplied them, so the
    result is independent of that order. Luma names its exports ``Name_-_Guests_-_<ts>.csv``,
    so filename order is export-timestamp order and the newest export supersedes on a tie.
    Sorted here rather than in each caller: a caller that feeds files in receive order would
    otherwise persist different rows for the same two exports.
    """

    @dataclass
    class _Group:
        sources: list[str] = field(default_factory=list)
        inferred_title: str | None = None
        guests_by_id: dict[str, LumaGuestRow] = field(default_factory=dict)
        warnings: list[str] = field(default_factory=list)

    grouped: dict[str, _Group] = {}
    for f in sorted(files, key=lambda pf: pf.filename):
        g = grouped.setdefault(f.guest_list.luma_event_id, _Group())
        g.sources.append(f.filename)
        if g.inferred_title is None:
            g.inferred_title = infer_title_from_filename(f.filename)
        for guest in f.guest_list.guests:
            existing = g.guests_by_id.get(guest.luma_guest_id)
            if existing is None or _supersedes(guest, existing):
                g.guests_by_id[guest.luma_guest_id] = guest
        g.warnings.extend(f"{f.filename}: {w}" for w in f.guest_list.warnings)

    events = [
        ResolvedEvent(eid, g.sources, g.inferred_title, list(g.guests_by_id.values()), g.warnings)
        for eid, g in grouped.items()
    ]
    all_guest_ids = {gu.luma_guest_id for ev in events for gu in ev.guests}
    all_emails = {gu.email for ev in events for gu in ev.guests if gu.email}
    return CombinerResult(events, len(all_guest_ids), len(all_emails))


__all__ = [
    "FEEDBACK_MAX_CHARS",
    "CombinerResult",
    "LumaEventGuestList",
    "LumaGuestRow",
    "LumaParseError",
    "ParsedLumaFile",
    "ResolvedEvent",
    "combine_luma_guest_lists",
    "infer_title_from_filename",
    "parse_luma_guest_list",
]
