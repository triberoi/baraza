"""One row per member: what they were invited to, what they turned up to, when.

The input records are deliberately not the Luma types from :mod:`baraza.ingest`, which
keeps scoring independent of any one platform's export format.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime

from baraza.ingest import GUEST_STATUSES

#: An event whose attendance was recorded: a door scan, or a Luma join link click on an
#: online event.
CHECKED_IN = "checked_in"

#: An event where no attendance was recorded. Registration is the only evidence, so a
#: committed guest counts as having attended and the event reports no show rate. Do not
#: give one a show rate: every committed guest reads as attended, so it is always 1.0.
REGISTRATION_ONLY = "registration_only"

#: Declared by the organizer, never inferred: an event with zero check-ins is either one
#: nobody scanned or one nobody came to, and the rows are identical.
ATTENDANCE_EVIDENCE = frozenset({CHECKED_IN, REGISTRATION_ONLY})


def effective_status(status: str, evidence: str) -> str:
    """``status`` as it must be read for an event carrying this evidence.

    On an unmeasured event a ``no_show`` becomes ``attended``; nothing else moves, because
    a waitlisted, cancelled or invited guest never said yes. Every rate in this package
    keys off ``"attended"``, so every one of them reads through here.
    """
    if evidence == REGISTRATION_ONLY and status == "no_show":
        return "attended"
    return status


@dataclass(frozen=True)
class EventRecord:
    """An event on the organizer's calendar. ``starts_at`` must be timezone-aware.

    :param ends_at: when the event finished, where that is known — a guest-list CSV does
        not carry one, only the API route does. ``None`` means "not recorded", never
        "instantaneous". Callers fall back to ``starts_at``, the same stand-in
        :func:`baraza.ingest.resolve_guest_status` uses to derive a no-show. The two must
        agree, or the rate and its statuses were decided by different cut-offs.
    """

    event_id: str
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    #: One of :data:`ATTENDANCE_EVIDENCE`.
    attendance_evidence: str = CHECKED_IN

    @property
    def over_by(self) -> datetime:
        """The instant after which this event's outcome is settled — its end when we
        have one, its start when we do not."""
        return self.ends_at or self.starts_at


@dataclass(frozen=True)
class AttendanceRecord:
    """One member's relationship to one event: a value from
:data:`baraza.ingest.GUEST_STATUSES`."""

    event_id: str
    member_id: str
    status: str
    #: Luma's own approval word, unmapped, where the caller has one. Reported, never
    #: measured: ``status`` is the only input to every rate. It carries the application
    #: funnel, which ``status`` cannot — an applicant nobody admitted and a real waitlist
    #: are both ``waitlisted``.
    luma_approval_status: str | None = None


@dataclass(frozen=True)
class MemberFacts:
    """Counted facts about one member.

    :param opportunities: settled events from this member's first appearance onward, so
        somebody who joined last month is not measured against the year before.
    """

    member_id: str
    first_seen_at: datetime
    last_attended_at: datetime | None
    events_attended: int
    no_shows: int
    opportunities: int
    #: How many of :attr:`events_attended` came from a :data:`REGISTRATION_ONLY` event.
    #: A screen showing the total must be able to say how much of it was measured.
    unmeasured_attendances: int = 0

    @property
    def attendance_rate(self) -> float:
        """Share of the events available to them that they attended, in ``[0, 1]``.
        ``0.0`` when none were available.

        Capped: a guest checked in before their event's official start counts in
        ``events_attended`` but not yet in ``opportunities``, which uncapped reads as
        attending 300% of the calendar."""
        if not self.opportunities:
            return 0.0
        return min(1.0, self.events_attended / self.opportunities)


def _require_aware(value: datetime, what: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{what} must be timezone-aware ({value!r} is naive). Mixing naive and aware datetimes "
            "raises deep inside the arithmetic; baraza.ingest produces UTC-aware values throughout."
        )


def validate_calendar(
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
    *,
    now: datetime | None = None,
) -> dict[str, datetime]:
    """Check a caller-assembled calendar and return ``event_id → starts_at``.

    Raises ``ValueError`` on a naive datetime, an unknown status or attendance-evidence
    value, an attendance naming an event not in ``events``, or either kind of duplicate. Rejecting beats dropping: a
    silent drop surfaces as a quietly wrong number. The store cannot emit a duplicate, but
    a consumer assembling these lists by hand can.
    """
    if now is not None:
        _require_aware(now, "now")

    starts: dict[str, datetime] = {}
    for event in events:
        _require_aware(event.starts_at, f"event {event.event_id}'s starts_at")
        if event.ends_at is not None:
            _require_aware(event.ends_at, f"event {event.event_id}'s ends_at")
        if event.attendance_evidence not in ATTENDANCE_EVIDENCE:
            raise ValueError(
                f"event {event.event_id!r} has attendance_evidence {event.attendance_evidence!r} — "
                f"expected one of {sorted(ATTENDANCE_EVIDENCE)}. Reading it either way gives a "
                "different answer for every rate in this package."
            )
        if event.event_id in starts:
            raise ValueError(
                f"event {event.event_id!r} appears twice in `events` — "
                "which of the two is right is a question only the caller can answer"
            )
        starts[event.event_id] = event.starts_at

    seen: set[tuple[str, str]] = set()
    for record in attendances:
        if record.status not in GUEST_STATUSES:
            raise ValueError(f"unknown status {record.status!r} — expected one of {sorted(GUEST_STATUSES)}")
        if record.event_id not in starts:
            raise ValueError(f"attendance record names event {record.event_id!r}, which is not in `events`")
        pair = (record.event_id, record.member_id)
        if pair in seen:
            raise ValueError(
                f"member {record.member_id!r} appears twice for event {record.event_id!r} — "
                "counting both would inflate attendance and move their lifecycle label"
            )
        seen.add(pair)
    return starts


def member_facts(
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
    *,
    now: datetime,
) -> list[MemberFacts]:
    """Count each member up, in ``member_id`` order. Anyone in ``attendances`` appears,
    including someone who only ever cancelled."""
    starts = validate_calendar(events, attendances, now=now)
    # Settled events, sorted once, so each member's opportunity count is a slice of this
    # list. Cut on `over_by` to match `resolve_guest_status`. The values are still START
    # times, so the `bisect_left` below compares like with like.
    settled = sorted(starts[event.event_id] for event in events if event.over_by <= now)

    evidence = {event.event_id: event.attendance_evidence for event in events}

    by_member: dict[str, list[AttendanceRecord]] = {}
    for record in attendances:
        by_member.setdefault(record.member_id, []).append(record)

    facts = []
    for member_id, records in by_member.items():
        first_seen_at = min(starts[r.event_id] for r in records)
        # Read through the evidence once, before anything is counted.
        seen = [(r, effective_status(r.status, evidence[r.event_id])) for r in records]
        # `no_shows` reads the stored status, frozen at import. An export taken mid-event
        # records the not-yet-arrived as "registered", so a snapshot never re-imported
        # undercounts them — settled by re-importing after the event, not re-derived here
        # (see `views.event_attendance` and the README for why).
        attended = [starts[r.event_id] for r, status in seen if status == "attended"]
        facts.append(
            MemberFacts(
                member_id=member_id,
                first_seen_at=first_seen_at,
                last_attended_at=max(attended) if attended else None,
                events_attended=len(attended),
                no_shows=sum(1 for _, status in seen if status == "no_show"),
                opportunities=len(settled) - bisect_left(settled, first_seen_at),
                unmeasured_attendances=sum(
                    1 for r, status in seen if status == "attended" and evidence[r.event_id] == REGISTRATION_ONLY
                ),
            )
        )
    return sorted(facts, key=lambda f: f.member_id)


__all__ = [
    "ATTENDANCE_EVIDENCE",
    "CHECKED_IN",
    "REGISTRATION_ONLY",
    "AttendanceRecord",
    "EventRecord",
    "MemberFacts",
    "effective_status",
    "member_facts",
    "validate_calendar",
]
