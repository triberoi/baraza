"""The three roll-ups the screens are drawn from: per event, per label, per person.

A view that cannot answer returns ``None``, never zero — a chart draws zero as a collapse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from baraza.analytics.members import AttendanceRecord, EventRecord, validate_calendar
from baraza.analytics.scoring import LIFECYCLE_LABELS, ScoredMember


@dataclass(frozen=True)
class EventAttendance:
    """One event's turnout, split into the numbers an organizer acts on.

    :param roster: everyone with any record for the event, cancellations included.
    :param committed: those who said yes to an event that then happened —
        ``attended + no_shows``, the denominator for a show rate.
    :param new_attendees: attendees for whom this was their first event.
        ``new + returning == attended`` by construction.

    The three application-funnel counts are ``None`` together, for an event that shows no
    sign of having required approval. They are read from Luma's own word rather than from
    :attr:`no_shows` and friends, because the six-value status cannot carry them: an
    applicant nobody admitted and a real waitlist are both ``waitlisted``.

    :param applied: how many asked for a place — everyone Luma did not record as invited
        by the host.
    :param not_admitted: of those, the ones left undecided at the event's end. Luma
        exposes no way to separate an organizer who declined to act from one who ran out
        of time, hit capacity, or never saw the request.
    :param declined_or_withdrew: of those, the ones Luma marked ``declined`` — which is
        the SAME word for an organizer declining an application and a guest choosing "Not
        Going". The two cannot be told apart, which is why this is not called "denied".
    """

    event_id: str
    title: str
    starts_at: datetime
    roster: int
    committed: int
    attended: int
    no_shows: int
    new_attendees: int
    returning_attendees: int
    applied: int | None = None
    not_admitted: int | None = None
    declined_or_withdrew: int | None = None

    @property
    def show_rate(self) -> float | None:
        """Share of those who said yes that turned up — ``None`` when nobody said yes.

        A turnout of zero against a real roster still reports ``0.0``: that one is a
        collapse and must keep saying so.
        """
        return self.attended / self.committed if self.committed else None


@dataclass(frozen=True)
class TimelineEntry:
    """One event on one member's history. ``status`` is ``None`` when the member has no
    record for it, which is not the same as cancelling."""

    event_id: str
    title: str
    starts_at: datetime
    status: str | None


def event_attendance(
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
    *,
    now: datetime,
) -> list[EventAttendance]:
    """Per-event turnout in calendar order, for events that are over.

    The cut is :attr:`EventRecord.over_by`, never ``starts_at``: a no-show is derived, so
    before an event ends nobody can be one and the show rate is 1.0 by construction.
    "New" means the member's first attended event.

    No-shows are read from the stored status, which is frozen at import time. An import
    taken while the event was still running records the not-yet-arrived as ``"registered"``,
    not ``"no_show"``, so a mid-event snapshot that is never re-imported reads here as a
    settled event with zero no-shows and a 100% show rate. The fix is to re-import once the
    event is over (README, "Export a guest list after its event has ended"); it is left to
    a re-import rather than re-derived at read time on purpose — a snapshot cannot tell a
    genuine no-show from a guest who checked in after it was taken, and inventing one would
    mark a real attendee absent.
    """
    starts = validate_calendar(events, attendances, now=now)
    started = [event for event in events if event.over_by <= now]

    # Ties broken by event_id: two events at the same instant must order the same way
    # on every run, or who counts as "new" moves between them.
    def order(record: AttendanceRecord) -> tuple[datetime, str]:
        return starts[record.event_id], record.event_id

    first_attendance: dict[str, tuple[datetime, str]] = {}
    for record in sorted((r for r in attendances if r.status == "attended"), key=order):
        first_attendance.setdefault(record.member_id, order(record))

    by_event: dict[str, list[AttendanceRecord]] = {event.event_id: [] for event in started}
    for record in attendances:
        if record.event_id in by_event:
            by_event[record.event_id].append(record)

    rows = []
    for event in sorted(started, key=lambda e: (e.starts_at, e.event_id)):
        records = by_event[event.event_id]
        attended = [r for r in records if r.status == "attended"]
        key = (event.starts_at, event.event_id)
        new = sum(1 for r in attended if first_attendance.get(r.member_id) == key)
        no_shows = sum(1 for r in records if r.status == "no_show")
        rows.append(
            EventAttendance(
                event_id=event.event_id,
                title=event.title,
                starts_at=event.starts_at,
                roster=len(records),
                committed=len(attended) + no_shows,
                attended=len(attended),
                no_shows=no_shows,
                new_attendees=new,
                returning_attendees=len(attended) - new,
                **_application_funnel(records),
            )
        )
    return rows


#: Luma words that only appear once an event requires approval. Their presence is what
#: distinguishes a gated event from an open one; nothing else in the export says so.
_APPLICATION_EVIDENCE = frozenset({"pending_approval", "requested", "declined"})

#: Asked for a place and never got an answer. `declined` is deliberately NOT here: it is
#: an answer, even though which answer cannot be known.
_UNDECIDED = frozenset({"pending_approval", "requested"})


def _application_funnel(records: list[AttendanceRecord]) -> dict[str, int | None]:
    """The three funnel counts, or three ``None``s for an event with no approval step.

    All three or none: a count of declines beside a blank total states something the data
    does not say. An event whose applicants were all approved carries none of the evidence
    words and reports nothing rather than zeros.
    """
    words = [r.luma_approval_status for r in records if r.luma_approval_status]
    if not _APPLICATION_EVIDENCE.intersection(words):
        return {"applied": None, "not_admitted": None, "declined_or_withdrew": None}
    return {
        "applied": sum(1 for w in words if w != "invited"),
        "not_admitted": sum(1 for w in words if w in _UNDECIDED),
        "declined_or_withdrew": sum(1 for w in words if w == "declined"),
    }


def lifecycle_mix(members: list[ScoredMember]) -> dict[str, int]:
    """How many members carry each label, in :data:`LIFECYCLE_LABELS` order. Zero-filled,
    so an empty label holds its place in the ordinal bar."""
    counts = dict.fromkeys(LIFECYCLE_LABELS, 0)
    for member in members:
        counts[member.lifecycle] += 1
    return counts


def member_timeline(
    member_id: str,
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
) -> list[TimelineEntry]:
    """One member's history from their first appearance onward, including the events they
    ignored — that is what makes someone going quiet visible.

    No clock, so events after ``now`` are included. Raises ``ValueError`` if the member has
    no records, since an empty timeline reads like a mistyped id.
    """
    starts = validate_calendar(events, attendances)
    theirs = {r.event_id: r.status for r in attendances if r.member_id == member_id}
    if not theirs:
        raise ValueError(f"no attendance records for member {member_id!r}")

    first_seen_at = min(starts[event_id] for event_id in theirs)
    return [
        TimelineEntry(
            event_id=event.event_id,
            title=event.title,
            starts_at=event.starts_at,
            status=theirs.get(event.event_id),
        )
        for event in sorted(events, key=lambda e: (e.starts_at, e.event_id))
        if event.starts_at >= first_seen_at
    ]


__all__ = [
    "EventAttendance",
    "TimelineEntry",
    "event_attendance",
    "lifecycle_mix",
    "member_timeline",
]
