"""The cohort retention grid: group members by the month they first attended, then
count how many came back each month after.

A cell is ``None``, never zero, when no event ran that month or the month has not
finished — a part-elapsed month is not a denominator yet. Column 0 is the cohort size by
definition. Members who never attended have no cohort and are absent.

Months are UTC, so an event just after midnight falls in the previous local month for an
organizer west of it. Fixing that needs a display timezone, which belongs with the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from baraza.analytics.members import AttendanceRecord, EventRecord, validate_calendar

_Month = tuple[int, int]


def _month_of(moment: datetime) -> _Month:
    """The UTC month a moment falls in. Converts first — reading ``.year``/``.month`` off
    a non-UTC datetime can put "this month" ahead of the events."""
    utc = moment.astimezone(UTC)
    return utc.year, utc.month


def _label(month: _Month) -> str:
    return f"{month[0]:04d}-{month[1]:02d}"


def _shift(month: _Month, offset: int) -> _Month:
    total = month[0] * 12 + (month[1] - 1) + offset
    return total // 12, total % 12 + 1


def _distance(start: _Month, end: _Month) -> int:
    return (end[0] - start[0]) * 12 + (end[1] - start[1])


@dataclass(frozen=True)
class Cohort:
    """Everyone who first attended in the same month, and what became of them.

    :param attended: one entry per month from the cohort's own month onward.
        ``attended[0]`` is always :attr:`size`. ``None`` means the question could not be
        asked that month.
    """

    month: str
    size: int
    attended: tuple[int | None, ...]

    def rate(self, offset: int) -> float | None:
        """Share of the cohort that came back ``offset`` months later, or ``None``."""
        if not 0 <= offset < len(self.attended):
            return None
        count = self.attended[offset]
        return None if count is None else count / self.size


@dataclass(frozen=True)
class RetentionGrid:
    """The whole grid, plus what the UI needs to explain its gaps.

    :param months: every month from the earliest cohort to ``now``, gaps included.
    :param active_months: those with at least one event, intersected with the grid. The
        difference from :attr:`months` is the ``None`` cells meaning "no event".
    :param in_progress_month: the unfinished current month, whose ``None`` cells mean
        "not yet counted" — a different gap the legend must name separately.
    """

    months: tuple[str, ...]
    active_months: frozenset[str]
    cohorts: tuple[Cohort, ...]
    in_progress_month: str | None = None


def retention_grid(
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
    *,
    now: datetime,
) -> RetentionGrid:
    """Build the cohort grid. A calendar nobody attended has no cohorts, not an error."""
    validate_calendar(events, attendances, now=now)

    # Cut on `over_by`, matching `member_facts`, `score_members` and the turnout views: a
    # no-show is derived at the event's END, so an event that has started but not finished
    # has no settled attendance to count. Only the CUT uses `over_by` — a cohort is still
    # bucketed by the month the event STARTED, which is the month a person attended in.
    settled = {event.event_id: event.starts_at for event in events if event.over_by <= now}

    attended_months: dict[str, set[_Month]] = {}
    for record in attendances:
        if record.status == "attended" and record.event_id in settled:
            attended_months.setdefault(record.member_id, set()).add(_month_of(settled[record.event_id]))
    if not attended_months:
        return RetentionGrid(months=(), active_months=frozenset(), cohorts=())

    active = {_month_of(start) for start in settled.values()}
    first_month = min(min(months) for months in attended_months.values())
    last_month = _month_of(now)
    span = _distance(first_month, last_month)
    months = tuple(_shift(first_month, i) for i in range(span + 1))

    # Stays a column (its cohort is real) but every backward-looking cell is None.
    in_progress = last_month

    cohorts: dict[_Month, list[str]] = {}
    for member_id, months_attended in attended_months.items():
        cohorts.setdefault(min(months_attended), []).append(member_id)

    rows = []
    for cohort_month in sorted(cohorts):
        members = cohorts[cohort_month]
        row: list[int | None] = []
        for offset in range(_distance(cohort_month, last_month) + 1):
            month = _shift(cohort_month, offset)
            if offset == 0:
                # Size by definition, so an unfinished month does not withhold it.
                row.append(len(members))
                continue
            if month == in_progress or month not in active:
                row.append(None)
                continue
            row.append(sum(1 for member_id in members if month in attended_months[member_id]))
        rows.append(Cohort(month=_label(cohort_month), size=len(members), attended=tuple(row)))

    on_the_grid = set(months)
    return RetentionGrid(
        months=tuple(_label(month) for month in months),
        # `active` covers the whole calendar, which can start before the first cohort.
        active_months=frozenset(_label(month) for month in active & on_the_grid),
        cohorts=tuple(rows),
        in_progress_month=_label(in_progress) if in_progress in on_the_grid else None,
    )


__all__ = ["Cohort", "RetentionGrid", "retention_grid"]
