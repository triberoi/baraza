"""Per-event cohort return (``baraza.analytics.views.event_cohorts``).

The question is "of the people in the room at this event, how many came to a later one",
and almost every way of getting it wrong produces a plausible number rather than an
obvious error. So the tests below pin the three choices that decide what it means:

1. the cohort is the whole room, not the first-timers in it;
2. the newest event reports nothing, never zero;
3. "later" is the same ordering the turnout table uses, down to the tie-break.
"""

from datetime import UTC, datetime, timedelta

import pytest

from baraza.analytics import REGISTRATION_ONLY, AttendanceRecord, EventRecord, event_attendance, event_cohorts

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

#: Four past events a month apart, plus one that has not happened. The future one is
#: explicit rather than the tail of the range: an off-by-one that put it exactly on `now`
#: would make it settled, and every "not somewhere to return to" test below would pass
#: while testing nothing.
EVENTS = [EventRecord(f"e{i}", f"Event {i}", NOW - timedelta(days=30 * (4 - i))) for i in range(4)] + [
    EventRecord("e4", "Next month", NOW + timedelta(days=30))
]


def _a(event: str, member: str, status: str = "attended") -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


def test_the_newest_event_reports_nothing_rather_than_zero() -> None:
    """It has had nothing to return to. A cohort that has had no chance to return is not a
    cohort that failed to, and rendering it as 0% would report a collapse every time an
    organizer imports the event they just ran."""
    rows = event_cohorts(EVENTS, [_a("e0", "m"), _a("e3", "m")], now=NOW)
    assert rows[-1].event_id == "e3"
    assert rows[-1].returned is None
    assert rows[-1].return_rate is None
    assert rows[-1].attended == 1, "its room is still real and still worth seeing"


def test_a_room_that_never_came_back_reports_zero() -> None:
    """The other side of the test above, and the reason it cannot simply blank a low
    number: nobody returning IS the finding, and it has to survive to the screen."""
    rows = event_cohorts(EVENTS, [_a("e0", "m"), _a("e1", "other")], now=NOW)
    assert (rows[0].returned, rows[0].return_rate) == (0, 0.0)


def test_the_cohort_is_the_whole_room_not_the_newcomers_in_it() -> None:
    """A regular coming back is the same evidence as a newcomer coming back. Counting only
    first-timers would answer the month grid's question again, and shrink every denominator
    on a calendar with any loyal core at all."""
    records = [_a("e0", "veteran"), _a("e1", "veteran"), _a("e1", "newcomer"), _a("e2", "newcomer")]
    rows = event_cohorts(EVENTS, records, now=NOW)

    second = next(r for r in rows if r.event_id == "e1")
    assert second.attended == 2, "the veteran is in this room too"
    assert second.returned == 1


def test_returning_is_any_later_event_not_the_next_one() -> None:
    """Somebody who skips one event and comes back to the one after has returned. Reading
    only the following event would report a series as dead every time it ran two events
    close together."""
    rows = event_cohorts(EVENTS, [_a("e0", "m"), _a("e2", "m")], now=NOW)
    assert rows[0].returned == 1


def test_a_member_returning_twice_is_counted_once() -> None:
    """The numerator is people, not visits — it shares a denominator with `attended`, and
    counting visits lets a rate exceed 100%."""
    rows = event_cohorts(EVENTS, [_a("e0", "m"), _a("e1", "m"), _a("e2", "m")], now=NOW)
    assert rows[0].returned == 1
    assert rows[0].return_rate == pytest.approx(1.0)


def test_events_that_have_not_happened_are_not_somewhere_to_return_to() -> None:
    """`e4` is in the future. If it counted as a later event the newest PAST event would
    stop reporting `None` and start reporting a real 0%, which is the blank-versus-zero
    distinction collapsing by the back door."""
    rows = event_cohorts(EVENTS, [_a("e0", "m"), _a("e3", "m")], now=NOW)
    assert [r.event_id for r in rows] == ["e0", "e1", "e2", "e3"]
    assert rows[-1].returned is None


def test_an_empty_room_asks_nothing() -> None:
    """Zero attended is not zero returned: there was nobody to ask about. A rate of 0/0
    rendered as 0% invents a failure out of an event whose guest list never arrived."""
    records = [_a("e0", "a", "no_show"), _a("e1", "b")]
    rows = event_cohorts(EVENTS, records, now=NOW)
    assert rows[0].attended == 0
    assert rows[0].return_rate is None


def test_the_worked_example_from_the_issue() -> None:
    """The shape the feature was filed against: a first room that sticks, then a settled
    rate. Asserted end to end so the arithmetic cannot drift from what was promised."""
    records = (
        [_a("e0", f"a{i}") for i in range(10)]
        + [_a("e1", f"a{i}") for i in range(4)]  # 4 of the first room return here
        + [_a("e1", f"b{i}") for i in range(6)]
        + [_a("e2", f"a{i}") for i in range(4, 6)]  # 2 more of the first room
        + [_a("e2", f"b{i}") for i in range(3)]
    )
    rows = event_cohorts(EVENTS, records, now=NOW)
    assert (rows[0].attended, rows[0].returned) == (10, 6)
    assert rows[0].return_rate == pytest.approx(0.6)


def test_an_unmeasured_event_takes_part_on_both_sides() -> None:
    """The decision the mode rests on, as a test: a registration on an event that took no
    attendance is a returning attendee. It counts INTO a later cohort's numerator and forms
    a cohort of its own — the mode withholds the turnout rate, never the person."""
    events = [
        EventRecord("e0", "Scanned", NOW - timedelta(days=60)),
        EventRecord("e1", "Livestream", NOW - timedelta(days=30), attendance_evidence=REGISTRATION_ONLY),
        EventRecord("e2", "Scanned again", NOW - timedelta(days=1)),
    ]
    records = [_a("e0", "m"), _a("e1", "m", "no_show"), _a("e1", "streamer", "no_show"), _a("e2", "streamer")]

    first, livestream, _last = event_cohorts(events, records, now=NOW)
    assert first.returned == 1, "their return landed on the livestream and still counts"
    assert livestream.attended == 2, "the room is who said yes, since nobody was at a door"
    assert livestream.returned == 1
    assert livestream.attendance_evidence == REGISTRATION_ONLY, "the row has to be markable"


def test_the_room_size_matches_the_turnout_table_row_for_row() -> None:
    """The two tables sit two clicks apart and are read against each other. Same settled
    cut, same evidence, same ordering — so a number here can be reconciled with the
    overview rather than starting an argument with it."""
    records = [_a("e0", "a"), _a("e0", "b"), _a("e1", "a"), _a("e2", "c", "no_show")]
    turnout = event_attendance(EVENTS, records, now=NOW)
    cohorts = event_cohorts(EVENTS, records, now=NOW)

    assert [(r.event_id, r.attended) for r in turnout] == [(c.event_id, c.attended) for c in cohorts]


def test_two_events_at_the_same_instant_order_the_same_way_here_as_in_turnout() -> None:
    """Ties break on event_id in both, so "later" means one thing. If the two disagreed, a
    member could be returning on one screen and not on the other from the same store."""
    same = D(2026, 4, 1)
    events = [
        EventRecord("e_b", "Second by id", same),
        EventRecord("e_a", "First by id", same),
        EventRecord("e_z", "Later", NOW - timedelta(days=1)),
    ]
    records = [_a("e_a", "m"), _a("e_b", "m")]
    rows = event_cohorts(events, records, now=NOW)

    assert [r.event_id for r in rows] == ["e_a", "e_b", "e_z"]
    assert rows[0].returned == 1, "e_a is first, so e_b is a return"
    assert rows[1].returned == 0, "e_b is second, and nothing after it was attended"
