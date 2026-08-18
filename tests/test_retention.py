"""Tests for the cohort retention grid (``baraza.analytics.retention``).

The grid's whole value is that a cell can be read at a glance, so most of what is
defended here is the two cases where it must **refuse** to report a number: a month
with no events, and a month that has not happened. Both would otherwise render as
zero, and zero in a retention grid means "they all left".
"""

from datetime import UTC, datetime, timedelta, timezone

from baraza.analytics.members import AttendanceRecord, EventRecord
from baraza.analytics.retention import retention_grid

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 15)

#: One event on the 10th of each month, January to June — the month NOW sits in.
MONTHLY = [EventRecord(f"m{i}", f"Meetup {i}", D(2026, i, 10)) for i in range(1, 7)]


def _a(event: str, member: str, status: str = "attended") -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


def test_a_cohort_is_keyed_on_first_attendance_and_starts_at_full_strength() -> None:
    """Column 0 is the cohort by construction, which is what makes it a legible 100%
    baseline for the rest of the row."""
    records = [_a("m1", "a"), _a("m1", "b"), _a("m3", "c")]
    grid = retention_grid(MONTHLY, records, now=NOW)
    assert [(c.month, c.size, c.attended[0]) for c in grid.cohorts] == [("2026-01", 2, 2), ("2026-03", 1, 1)]
    assert all(c.rate(0) == 1.0 for c in grid.cohorts)


def test_registering_without_attending_earns_no_cohort() -> None:
    """The grid is keyed on first attendance. Someone who only ever no-showed belongs on
    the roster as a prospect, not in a row measuring who came back."""
    grid = retention_grid(MONTHLY, [_a("m1", "ghost", "no_show"), _a("m2", "ghost", "cancelled")], now=NOW)
    assert grid.cohorts == () and grid.months == ()


def test_a_month_with_no_event_is_none_rather_than_zero() -> None:
    """The lapse guard in another shape. An organizer who skipped a month did not lose
    that month's members, and a zero column paints exactly that."""
    calendar = [e for e in MONTHLY if e.starts_at.month != 3]  # March skipped
    records = [_a("m1", "a"), _a("m2", "a"), _a("m4", "a")]
    grid = retention_grid(calendar, records, now=NOW)
    (cohort,) = grid.cohorts

    assert grid.months == ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06")
    assert "2026-03" not in grid.active_months
    assert cohort.attended[2] is None and cohort.rate(2) is None
    assert cohort.attended[1] == 1 and cohort.attended[3] == 1


def test_a_month_that_has_not_happened_is_off_the_end_of_the_row() -> None:
    """Cells beyond ``now`` are absent rather than empty, so the newest cohort's short
    row reads as "not yet" instead of as a cliff."""
    calendar = [*MONTHLY, EventRecord("m7", "Meetup 7", D(2026, 7, 10))]
    grid = retention_grid(calendar, [_a("m1", "a")], now=NOW)
    (cohort,) = grid.cohorts
    assert grid.months[-1] == "2026-06"
    assert len(cohort.attended) == 6
    assert cohort.rate(6) is None


def test_the_row_length_is_the_months_since_that_cohort_began() -> None:
    """A triangle, not a rectangle: a cohort that started in May has one month of history
    to show, and padding it out to January's width would invent four empty cells."""
    records = [_a("m1", "jan"), _a("m5", "may")]
    grid = retention_grid(MONTHLY, records, now=NOW)
    assert [(c.month, len(c.attended)) for c in grid.cohorts] == [("2026-01", 6), ("2026-05", 2)]


def test_a_cell_counts_members_not_attendances() -> None:
    """Two events in one month must not let a member retain twice — the rate would exceed
    100% and the grid would stop being readable as a share."""
    calendar = [*MONTHLY, EventRecord("extra", "Second in Feb", D(2026, 2, 20))]
    records = [_a("m1", "a"), _a("m2", "a"), _a("extra", "a")]
    grid = retention_grid(calendar, records, now=NOW)
    (cohort,) = grid.cohorts
    assert cohort.attended[1] == 1
    assert cohort.rate(1) == 1.0


def test_a_rate_is_never_above_one_or_below_zero() -> None:
    """Swept across a grid with re-attendance, gaps and a late cohort, because the rate
    is rendered as a bar and an out-of-range value is a broken UI, not a wrong number."""
    calendar = [*MONTHLY, EventRecord("extra", "Second in Feb", D(2026, 2, 20))]
    records = [
        *(_a(e.event_id, "loyal") for e in calendar),
        _a("m2", "spring"),
        _a("extra", "spring"),
        _a("m5", "late"),
        _a("m3", "lapser"),
    ]
    grid = retention_grid(calendar, records, now=NOW)
    for cohort in grid.cohorts:
        for offset in range(-1, len(cohort.attended) + 2):
            rate = cohort.rate(offset)
            assert rate is None or 0.0 <= rate <= 1.0, f"{cohort.month} offset {offset} → {rate}"


def test_the_gaps_are_explainable_from_the_grid_alone() -> None:
    """Every blank cell has a reason the grid itself carries, and a legend that cannot
    name the reason cannot explain its own gaps.

    There are three, and the test asserts the split **in both directions** — that each
    reason produces a blank, and that no blank lacks a reason. One-directional was how
    the in-progress month hid: it became a third kind of blank and nothing here
    noticed, because the only assertion ran from reason to cell and never back.
    """
    calendar = [e for e in MONTHLY if e.starts_at.month not in (3, 4)]
    grid = retention_grid(calendar, [_a("m1", "a"), _a("m5", "b")], now=NOW)
    assert set(grid.months) - grid.active_months == {"2026-03", "2026-04"}
    assert grid.in_progress_month == "2026-06"

    for cohort in grid.cohorts:
        start = grid.months.index(cohort.month)
        for offset, count in enumerate(cohort.attended):
            month = grid.months[start + offset]
            no_event = month not in grid.active_months
            not_yet_counted = month == grid.in_progress_month
            # Column 0 is a definition, not a measurement, so it is never blank.
            if offset == 0:
                assert count is not None
            elif no_event or not_yet_counted:
                assert count is None, f"{month} should be blank"
            else:
                assert count is not None, f"{month} is blank for no reason the grid gives"


def test_attendance_after_now_does_not_reach_the_grid() -> None:
    """A check-in recorded against an event that has not started is a data oddity the
    score already caps for. It must not extend the grid past ``now`` either."""
    calendar = [*MONTHLY, EventRecord("m8", "Meetup 8", D(2026, 8, 10))]
    grid = retention_grid(calendar, [_a("m1", "a"), _a("m8", "a")], now=NOW)
    assert grid.months[-1] == "2026-06"
    assert len(grid.cohorts[0].attended) == 6


def test_an_empty_calendar_is_an_empty_grid() -> None:
    assert retention_grid([], [], now=NOW) == retention_grid(MONTHLY, [], now=NOW)


def test_the_grid_is_deterministic() -> None:
    """Same data, same grid, whatever order it arrives in — the PRD's determinism
    criterion at the level a UI and an export both consume."""
    records = [_a("m1", "a"), _a("m3", "b"), _a("m4", "a"), _a("m6", "b")]
    assert retention_grid(MONTHLY, records, now=NOW) == retention_grid(MONTHLY, list(reversed(records)), now=NOW)


def test_a_cohort_spanning_a_year_end_counts_months_not_january() -> None:
    """Month arithmetic across a December boundary — the case a naive
    ``end.month - start.month`` gets wrong by twelve."""
    calendar = [EventRecord(f"y{i}", f"E{i}", D(2025, 11, 10) + timedelta(days=30 * i)) for i in range(5)]
    grid = retention_grid(calendar, [_a("y0", "a"), _a("y4", "a")], now=D(2026, 4, 1))
    (cohort,) = grid.cohorts
    assert cohort.month == "2025-11"
    assert grid.months == ("2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04")
    assert cohort.attended[0] == 1 and cohort.attended[4] == 1


def test_the_month_still_running_is_not_reported_as_a_collapse() -> None:
    """Only entirely-future months used to be withheld, so the CURRENT
    part-elapsed month was counted like any finished one — and it is small because the
    month is young, not because anyone left. Every row therefore ended in what read as
    a fall from 100% to 0%, and it healed itself as the month went on, which is what
    made it so believable.

    A month is a denominator. Two thirds of one is not a smaller month; it is not a
    month yet.
    """
    # NOW is 2026-06-15 and June's event was on the 10th, so June is active, in the
    # past, and unfinished — the exact cell the defect painted black.
    records = [_a("m1", "a"), _a("m2", "a"), _a("m3", "a"), _a("m4", "a"), _a("m5", "a")]
    grid = retention_grid(MONTHLY, records, now=NOW)
    (cohort,) = grid.cohorts

    assert grid.months[-1] == "2026-06"
    assert grid.in_progress_month == "2026-06"
    assert cohort.attended[-1] is None, "June is still running — it has no answer yet"
    assert cohort.rate(len(cohort.attended) - 1) is None
    # ...and the finished months are untouched: this withholds one cell, not the row.
    assert cohort.attended[:5] == (1, 1, 1, 1, 1)


def test_a_cohort_formed_this_month_still_reports_its_own_size() -> None:
    """Column 0 is the cohort by construction — true the instant the cohort exists —
    so an unfinished month withholds the retention answers without hiding the fact
    that a group of people just arrived."""
    grid = retention_grid(MONTHLY, [_a("m6", "newcomer")], now=NOW)
    (cohort,) = grid.cohorts
    assert cohort.month == "2026-06"
    assert (cohort.size, cohort.attended) == (1, (1,))
    assert cohort.rate(0) == 1.0


def test_the_legend_never_names_a_month_the_grid_does_not_have() -> None:
    """``active_months`` was built from the whole calendar while ``months``
    starts at the first cohort, so an event before anyone's first attendance — one
    nobody came to, say — put a month in the legend that has no column. A legend
    pointing at a column that is not there is worse than no legend."""
    # An October event nobody attended, four months before the first cohort.
    calendar = [EventRecord("early", "Nobody came", D(2025, 10, 10)), *MONTHLY]
    grid = retention_grid(calendar, [_a("m2", "a"), _a("m4", "a")], now=NOW)

    assert "2025-10" not in grid.active_months
    assert set(grid.active_months) <= set(grid.months), "the legend must be a subset of the axis"
    assert grid.months[0] == "2026-02"


def test_the_legend_is_a_subset_of_the_axis_whatever_the_calendar_looks_like() -> None:
    """The property, rather than the one example above."""
    calendars = [
        MONTHLY,
        [EventRecord("early", "E", D(2024, 1, 1)), *MONTHLY],
        [*MONTHLY, EventRecord("late", "E", D(2026, 12, 1))],
    ]
    for calendar in calendars:
        for records in ([_a("m1", "a")], [_a("m6", "a")], [_a("m1", "a"), _a("m6", "b")]):
            grid = retention_grid(calendar, records, now=NOW)
            assert set(grid.active_months) <= set(grid.months)
            if grid.in_progress_month is not None:
                assert grid.in_progress_month in grid.months


def test_the_grid_is_the_same_whatever_timezone_the_clock_arrives_in() -> None:
    """The property this suite exists to establish: the same
    inputs give the same answer on any host, in any timezone, at any wall-clock time.

    ``_month_of`` read ``.year`` and ``.month`` straight off whatever it was handed. A
    caller passing ``now`` in UTC+13 put "this month" a month ahead of the events, so
    the span from the first cohort could go **negative** — ``range(span + 1)`` empty,
    an empty axis, and cohort rows still built against it. An empty grid with
    impossible contents, on nothing but the host's timezone.
    """
    records = [_a("m1", "a"), _a("m3", "a"), _a("m5", "b")]
    baseline = retention_grid(MONTHLY, records, now=NOW)

    for offset_hours in (-11, -8, -1, 1, 5, 13, 14):
        shifted = NOW.astimezone(timezone(timedelta(hours=offset_hours)))
        assert shifted == NOW, "the fixture must be the SAME instant, only spelled differently"
        assert retention_grid(MONTHLY, records, now=shifted) == baseline, (
            f"UTC{offset_hours:+d} gave a different grid for the same instant"
        )


def test_a_far_eastern_clock_does_not_empty_the_grid() -> None:
    """The failure mode above, named rather than only covered by the sweep: at the end
    of a month, a UTC+14 clock is already in the next one. Before the fix that is where
    the negative span came from, and the symptom was a grid with no columns at all."""
    end_of_month = D(2026, 6, 30, 23, 0)
    far_east = end_of_month.astimezone(timezone(timedelta(hours=14)))  # already 2026-07-01 there
    grid = retention_grid(MONTHLY, [_a("m1", "a")], now=far_east)

    assert grid.months, "the grid must not come back empty"
    assert grid.months[-1] == "2026-06"
    assert grid.cohorts and len(grid.cohorts[0].attended) == len(grid.months)


def test_an_event_that_has_started_but_not_finished_is_not_counted_yet() -> None:
    """The cut is `over_by`, matching `member_facts`, `score_members` and the turnout
    views. Retention cut on `starts_at`, so a multi-day event that had begun counted
    before anyone knew who turned up — and a no-show is only derived at the event's end,
    which is precisely the fact the grid is built from.

    Stated as the straddling case because that is where the two cutoffs disagree by a
    whole COLUMN: an event running 30 June to 2 July is filed under June, so cutting on
    its start admits a June cell whose attendance is still being decided.
    """
    # January to May, so the straddling event is the ONLY thing that could make June
    # active. With MONTHLY's own 10 June meetup present the month is active either way
    # and the assertion passes without testing anything.
    straddling = EventRecord("m7", "Summit", D(2026, 6, 30), ends_at=D(2026, 7, 2))
    events = [*MONTHLY[:5], straddling]
    records = [_a("m1", "a"), _a("m7", "a")]

    mid_event = retention_grid(events, records, now=D(2026, 7, 1))
    assert "2026-06" not in mid_event.active_months, (
        "an event that has started but not finished must not make its month active"
    )

    after = retention_grid(events, records, now=D(2026, 7, 3))
    assert "2026-06" in after.active_months, "once it is over, the month counts"


def test_an_unfinished_event_influences_the_grid_exactly_as_much_as_its_absence() -> None:
    """The property under the case above, rather than one example of it, and stated so
    that month granularity cannot hide a difference: for any calendar and any instant,
    the grid must equal the grid built from the settled events ALONE. An unfinished event
    is not "counted carefully" — it is not counted.

    Asserting on `active_months` alone was too weak to fail on the old code: whenever a
    settled event shared a month with an unfinished one, the month was active either way.
    Comparing whole grids removes the granularity from the question.
    """
    events = [
        *MONTHLY,
        EventRecord("long", "Long", D(2026, 6, 1), ends_at=D(2026, 6, 20)),
        EventRecord("straddle", "Straddle", D(2026, 5, 31), ends_at=D(2026, 6, 2)),
        EventRecord("open", "Open-ended", D(2026, 6, 14)),
    ]
    records = [_a("m1", "a"), _a("long", "a"), _a("straddle", "b"), _a("open", "b"), _a("m3", "c")]

    for now in (D(2026, 6, 1), D(2026, 6, 10), D(2026, 6, 15), D(2026, 6, 21), D(2026, 7, 1)):
        settled = [e for e in events if e.over_by <= now]
        settled_ids = {e.event_id for e in settled}
        assert retention_grid(events, records, now=now) == retention_grid(
            settled, [r for r in records if r.event_id in settled_ids], now=now
        ), f"at {now}, an unfinished event changed the grid"
