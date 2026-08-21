"""Tests for the counting layer (``baraza.analytics.members``).

No judgments here — just whether the arithmetic under the labels counts the right
events. The interesting one is ``opportunities``: it is the denominator of both the
attendance rate and the champion test, so an off-by-one in it moves people between
labels rather than moving a number slightly.
"""

from datetime import UTC, datetime, timedelta

import pytest

from baraza.analytics.members import AttendanceRecord, EventRecord, member_facts

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

# A year of monthly events, the last two still in the future relative to NOW.
EVENTS = [EventRecord(f"e{i}", f"Event {i}", D(2026, 1, 1) + timedelta(days=30 * i)) for i in range(8)]


def _a(event: str, member: str, status: str) -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


def test_opportunities_start_when_the_member_does_and_stop_at_now() -> None:
    """The two boundaries in one test because they are one idea: a member is measured
    against the events they could actually have come to. Counting the calendar before
    they existed makes every new member look disengaged; counting events that have not
    happened makes everyone look worse the further ahead the organizer schedules."""
    started = [e for e in EVENTS if e.starts_at <= NOW]
    assert 0 < len(started) < len(EVENTS), "fixture must span NOW for this test to mean anything"

    early = member_facts(EVENTS, [_a("e0", "m", "attended")], now=NOW)[0]
    assert early.opportunities == len(started)

    late = member_facts(EVENTS, [_a(started[-1].event_id, "m", "attended")], now=NOW)[0]
    assert late.opportunities == 1

    future_only = member_facts(EVENTS, [_a(EVENTS[-1].event_id, "m", "registered")], now=NOW)[0]
    assert future_only.opportunities == 0


def test_a_member_who_only_cancelled_is_still_counted() -> None:
    """Dropping them would hide the cancellations, which is a thing the organizer wants
    to see. They have a first appearance and nothing else."""
    (facts,) = member_facts(EVENTS, [_a("e0", "m", "cancelled")], now=NOW)
    assert facts.events_attended == 0 and facts.no_shows == 0
    assert facts.last_attended_at is None
    assert facts.first_seen_at == EVENTS[0].starts_at
    assert facts.attendance_rate == 0.0


def test_first_seen_and_last_attended_come_from_different_sets() -> None:
    """``first_seen_at`` is any appearance; ``last_attended_at`` is an attendance only.
    A member who registered early, no-showed, then came once must not report the
    no-showed event as their last attendance."""
    (facts,) = member_facts(
        EVENTS,
        [_a("e0", "m", "no_show"), _a("e1", "m", "attended"), _a("e2", "m", "no_show")],
        now=NOW,
    )
    assert facts.first_seen_at == EVENTS[0].starts_at
    assert facts.last_attended_at == EVENTS[1].starts_at
    assert (facts.events_attended, facts.no_shows) == (1, 2)


def test_the_attendance_rate_has_no_zero_denominator() -> None:
    """The rate is read by the champion test on every member, so the member with no
    opportunities yet must return a number rather than raise."""
    (facts,) = member_facts(EVENTS, [_a(EVENTS[-1].event_id, "m", "registered")], now=NOW)
    assert facts.opportunities == 0
    assert facts.attendance_rate == 0.0


def test_the_attendance_rate_cannot_exceed_one() -> None:
    """The two counts come off different clocks. Luma sets ``checked_in_at`` when the
    guest scans, and doors open before the advertised start — so a member can be
    ``attended`` at an event that, at ``now``, has not started. They then count as
    attending more events than were available to them, and the rate goes over 1 and
    into the score. Found by the score sweep in ``test_scoring.py``, fixed here."""
    future = EVENTS[-1]
    assert future.starts_at > NOW
    (facts,) = member_facts(EVENTS, [_a(future.event_id, "m", "attended")], now=NOW)
    assert facts.events_attended == 1 and facts.opportunities == 0
    assert facts.attendance_rate == 0.0

    started = [e for e in EVENTS if e.starts_at <= NOW]
    records = [_a(started[-1].event_id, "m", "attended"), _a(future.event_id, "m", "attended")]
    (mixed,) = member_facts(EVENTS, records, now=NOW)
    assert mixed.events_attended == 2 and mixed.opportunities == 1
    assert mixed.attendance_rate == 1.0


def test_members_come_back_in_a_stable_order() -> None:
    """The tool writes these to a store and renders them in a table. Ordering that
    depended on input order would make two runs over the same folder produce different
    files, which is the determinism the tool promises."""
    records = [_a("e0", "zoe", "attended"), _a("e0", "amy", "attended"), _a("e1", "moe", "no_show")]
    assert [f.member_id for f in member_facts(EVENTS, records, now=NOW)] == ["amy", "moe", "zoe"]
    assert member_facts(EVENTS, list(reversed(records)), now=NOW) == member_facts(EVENTS, records, now=NOW)


# --- the caller's assembly mistakes, which must not be silent -------------------
def test_an_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        member_facts(EVENTS, [_a("e0", "m", "showed_up")], now=NOW)


def test_an_attendance_for_an_unknown_event_is_rejected() -> None:
    """Silently dropping it would shrink someone's history by one event and read as a
    quietly wrong number rather than as an error."""
    with pytest.raises(ValueError, match="not in `events`"):
        member_facts(EVENTS, [_a("nope", "m", "attended")], now=NOW)


@pytest.mark.parametrize("naive", ["now", "event"])
def test_a_naive_datetime_is_rejected_with_its_name(naive: str) -> None:
    """Mixing naive and aware datetimes raises from inside the arithmetic with a message
    that names neither the field nor the caller. Both entry points are checked up front."""
    events = [EventRecord("e0", "E", datetime(2026, 1, 1))] if naive == "event" else EVENTS
    now = datetime(2026, 6, 1) if naive == "now" else NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        member_facts(events, [_a("e0", "m", "attended")], now=now)


def test_a_naive_ends_at_is_rejected_too() -> None:
    """``ends_at`` is compared against ``now`` like any
    other timestamp, so it gets the same check. A field added to a validated record
    without being validated is how the next naive-datetime bug gets in."""
    events = [EventRecord("e0", "E", NOW - timedelta(days=1), ends_at=datetime(2026, 1, 1))]
    with pytest.raises(ValueError, match="timezone-aware"):
        member_facts(events, [_a("e0", "m", "attended")], now=NOW)


# --- guards on the published API ------------------------------------------------------
# These two are NOT live wrong output and were not budgeted as such. Nothing inside
# baraza can produce either — `events` is `event_id PRIMARY KEY` and `attendances` is
# `PRIMARY KEY (event_id, member_id)`, so `Store.calendar()` cannot emit a duplicate.
# But `baraza.analytics` is an importable, published API and a consumer assembling
# these lists by hand can, and nothing here was looking.


def test_the_same_event_listed_twice_is_rejected() -> None:
    """A repeated ``event_id`` was handled two contradictory ways in one
    request: the counting layer's ``{e.event_id: ...}`` silently kept the last one,
    while ``event_attendance`` emitted a row for each. Neither treated it as the mistake
    it is, so one calendar produced two different answers to "how many events" depending
    on which function was asked.

    Rejecting is the only response that does not require choosing which of the two is
    right, and that is a question only the caller can answer.
    """
    with pytest.raises(ValueError, match="appears twice in `events`"):
        member_facts([EVENTS[0], EVENTS[0]], [_a("e0", "m", "attended")], now=NOW)

    # A different event with the same start is fine — it is the ID that must be unique.
    twin = EventRecord("e0-twin", "Same slot", EVENTS[0].starts_at)
    member_facts([EVENTS[0], twin], [_a("e0", "m", "attended")], now=NOW)


def test_the_same_person_listed_twice_for_one_event_is_rejected() -> None:
    """A repeated ``(event_id, member_id)`` pair was appended twice and
    counted twice, so attendance inflated and the lifecycle label moved with it — a
    first-timer reading as a regular. The two records need not even agree with each
    other, which is what makes silently keeping one of them indefensible."""
    with pytest.raises(ValueError, match="appears twice for event"):
        member_facts(EVENTS, [_a("e0", "m", "attended"), _a("e0", "m", "attended")], now=NOW)
    with pytest.raises(ValueError, match="appears twice for event"):
        member_facts(EVENTS, [_a("e0", "m", "attended"), _a("e0", "m", "no_show")], now=NOW)

    # The same person at two events, and two people at one event, are both ordinary.
    facts = member_facts(
        EVENTS,
        [_a("e0", "m", "attended"), _a("e1", "m", "attended"), _a("e0", "other", "attended")],
        now=NOW,
    )
    assert {f.member_id: f.events_attended for f in facts} == {"m": 2, "other": 1}


# --- an event is an opportunity once it is OVER, not once it has begun ---
#
# `EventRecord.over_by` exists for this — "the instant after which this event's outcome
# is settled". The counting here used the START instead, so for the whole duration of
# every event a member who had attended everything was counted as having missed the one
# currently in the room. `views.py` already cuts on `over_by`; these two did not, and
# the fixture above cannot show it because none of its events carries an `ends_at`,
# which makes `over_by` and `starts_at` the same instant.

RUNNING_NOW = EventRecord("live", "Happening now", NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=2))
_LAST_MONTH = NOW - timedelta(days=30)
FINISHED = EventRecord("done", "Last month", _LAST_MONTH, ends_at=_LAST_MONTH + timedelta(hours=2))


def test_an_event_still_running_is_not_yet_an_opportunity_to_have_missed() -> None:
    facts = member_facts(
        [FINISHED, RUNNING_NOW],
        [_a("done", "m1", "attended"), _a("live", "m1", "registered")],
        now=NOW,
    )[0]

    assert facts.events_attended == 1
    assert facts.opportunities == 1  # the finished one only
    assert facts.attendance_rate == 1.0


def test_the_denominator_uses_the_same_cut_off_the_statuses_were_derived_with() -> None:
    """The property, stated where the module states it (`members.py` docstring): a
    turnout rate computed over statuses decided by one cut-off and a denominator decided
    by another is a number about nothing. `resolve_guest_status` settles a no-show at
    `ends_at or starts_at`, which is exactly `over_by`.

    Asserted by holding the calendar still and moving only the clock across the running
    event's end: the count may not change at the moment the event STARTS, only when it
    is over.
    """
    calendar = [FINISHED, RUNNING_NOW]
    marks = [_a("done", "m1", "attended"), _a("live", "m1", "registered")]

    before_it_started = member_facts(calendar, marks, now=RUNNING_NOW.starts_at - timedelta(minutes=1))[0]
    while_it_runs = member_facts(calendar, marks, now=NOW)[0]
    after_it_ended = member_facts(calendar, marks, now=RUNNING_NOW.ends_at + timedelta(minutes=1))[0]

    assert before_it_started.opportunities == while_it_runs.opportunities
    assert after_it_ended.opportunities == while_it_runs.opportunities + 1
