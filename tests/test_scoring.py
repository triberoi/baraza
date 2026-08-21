"""Tests for the tool's own score and lifecycle labels (``baraza.analytics.scoring``).

Two things are being defended here.

The first is that the **thresholds are real**. A rule that reads a threshold and then
decides by a hard-coded number would pass any fixed-example test; the tests below move
each threshold and require the label to move with it.

The second is the **published vocabulary**. ``LIFECYCLE_LABELS`` is what a store column
and a UI legend are built on, so "the function only ever returns one of these" is
asserted over a swept input space rather than over a handful of cases — and the set
itself is pinned, or the property holds vacuously the moment someone widens it.
"""

from datetime import UTC, datetime, timedelta

import pytest

from baraza.analytics.members import AttendanceRecord, EventRecord, MemberFacts, member_facts
from baraza.analytics.scoring import (
    CHAMPION,
    FIRST_TIMER,
    LAPSED,
    LIFECYCLE_LABELS,
    PROSPECT,
    REGULAR,
    analyze_members,
    lifecycle_label,
    recency,
    score_member,
)
from baraza.analytics.thresholds import Thresholds

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)
DEFAULTS = Thresholds()

#: Twelve monthly events, all in the past relative to NOW — so every member's
#: opportunity count is the whole calendar unless they joined late.
EVENTS = [EventRecord(f"e{i}", f"Event {i}", NOW - timedelta(days=30 * (12 - i))) for i in range(12)]


def _a(event: str, member: str, status: str) -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


def _facts(**overrides: object) -> MemberFacts:
    """A member built directly, so a test can state the counted facts it cares about
    without arranging a calendar that produces them."""
    base = dict(
        member_id="m",
        first_seen_at=NOW - timedelta(days=200),
        last_attended_at=NOW - timedelta(days=1),
        events_attended=1,
        no_shows=0,
        opportunities=1,
    )
    return MemberFacts(**{**base, **overrides})  # type: ignore[arg-type]


def _label(facts: MemberFacts, thresholds: Thresholds = DEFAULTS, *, missed_an_event: bool = True) -> str:
    return lifecycle_label(
        facts, thresholds=thresholds, now=NOW, an_event_happened_while_absent=missed_an_event
    )


# --- the vocabulary ------------------------------------------------------------
def test_the_label_is_always_one_of_the_published_set() -> None:
    """Swept over the input space the function branches on — attendance count, no-shows,
    opportunities, how long ago they last came, and whether the calendar ran — rather
    than over chosen examples. A store column and a UI legend are built on this set."""
    for attended in (0, 1, 2, 3, 4, 10):
        for no_shows in (0, 3):
            for opportunities in (0, 1, 4, 20):
                for days_ago in (0, 30, 91, 400):
                    for missed_an_event in (True, False):
                        facts = _facts(
                            last_attended_at=None if attended == 0 else NOW - timedelta(days=days_ago),
                            events_attended=attended,
                            no_shows=no_shows,
                            opportunities=opportunities,
                        )
                        label = _label(facts, missed_an_event=missed_an_event)
                        assert label in LIFECYCLE_LABELS, f"{facts} produced {label!r}, outside the published set"


def test_the_published_set_is_the_five() -> None:
    """Pins the set, so the property above cannot be satisfied by widening it. Four come
    are the ramp; ``prospect`` is the addition — someone who registered and
    never came is a real group, and calling them a first-timer would be false."""
    assert LIFECYCLE_LABELS == ("prospect", "first_timer", "regular", "champion", "lapsed")


# --- the labels themselves ------------------------------------------------------
def test_a_member_who_never_attended_is_a_prospect_however_often_they_registered() -> None:
    for no_shows in (1, 5, 50):
        facts = _facts(last_attended_at=None, events_attended=0, no_shows=no_shows, opportunities=no_shows)
        assert _label(facts) == PROSPECT


def test_the_ramp_moves_with_regular_min_events() -> None:
    """The boundary is the threshold's, not a hard-coded 2. Checked either side of it,
    at two different settings."""
    for minimum in (2, 5):
        thresholds = Thresholds(regular_min_events=minimum, champion_min_events=minimum + 10)
        below = _facts(events_attended=minimum - 1, opportunities=100)
        at = _facts(events_attended=minimum, opportunities=100)
        assert _label(below, thresholds) == FIRST_TIMER
        assert _label(at, thresholds) == REGULAR


def test_a_champion_must_clear_both_the_count_and_the_rate() -> None:
    """Two conditions on purpose: showing up a lot and showing up reliably are different
    claims, and the label means both. Someone who attended 20 of 200 is not a champion,
    and someone who attended 2 of 2 has not shown up a lot."""
    thresholds = Thresholds(regular_min_events=2, champion_min_events=4, champion_min_rate=0.5)
    assert _label(_facts(events_attended=4, opportunities=8), thresholds) == CHAMPION
    assert _label(_facts(events_attended=4, opportunities=9), thresholds) == REGULAR  # rate 0.44
    assert _label(_facts(events_attended=3, opportunities=3), thresholds) == REGULAR  # count short


def test_lapsed_needs_something_to_have_been_missed() -> None:
    """The guard that stops a quiet summer marking the whole roster lapsed. Same member,
    same silence — only whether an event actually happened while they were gone differs.

    The parameter is per-member; the guard itself is unchanged,
    and this test still asserts it from both sides."""
    stale = _facts(last_attended_at=NOW - timedelta(days=DEFAULTS.lapsed_after_days + 1), events_attended=6)
    assert _label(stale, missed_an_event=True) == LAPSED
    assert _label(stale, missed_an_event=False) == CHAMPION


def test_lapsing_moves_with_its_threshold() -> None:
    facts = _facts(last_attended_at=NOW - timedelta(days=100), events_attended=6)
    assert _label(facts, Thresholds(lapsed_after_days=90)) == LAPSED
    assert _label(facts, Thresholds(lapsed_after_days=180)) == CHAMPION


def test_lapsed_outranks_the_ramp() -> None:
    """A champion who stopped coming is lapsed, not a champion. The organizer needs to
    see them in the group they can act on."""
    facts = _facts(last_attended_at=NOW - timedelta(days=365), events_attended=50, opportunities=50)
    assert _label(facts) == LAPSED


# --- the score ------------------------------------------------------------------
def test_the_score_is_a_percentage_or_nothing() -> None:
    """Swept, for the same reason the label sweep exists: the score is rendered as a bar
    and written to a store, so a value outside 0-100 is a broken UI rather than a wrong
    number. ``None`` is the deliberate fourth state — see the module docstring."""
    for attended in (0, 1, 7):
        for no_shows in (0, 2, 9):
            for opportunities in (0, 1, 7, 40):
                for days_ago in (0, 45, 90, 91, 179, 180, 900):
                    facts = _facts(
                        last_attended_at=None if attended == 0 else NOW - timedelta(days=days_ago),
                        events_attended=attended,
                        no_shows=no_shows,
                        opportunities=opportunities,
                    )
                    score = score_member(facts, thresholds=DEFAULTS, now=NOW)
                    assert score is None or 0 <= score <= 100, f"{facts} scored {score}"


def test_no_data_scores_none_rather_than_zero() -> None:
    """A member whose only event has not happened yet has no record, which is a different
    fact from a bad one. A UI that renders both as 0 is lying about the second."""
    assert score_member(_facts(opportunities=0), thresholds=DEFAULTS, now=NOW) is None
    assert score_member(_facts(opportunities=1), thresholds=DEFAULTS, now=NOW) is not None


def test_the_ends_of_the_scale_are_reachable() -> None:
    """Full marks for attending everything, recently, having never no-showed; zero for
    having had chances and taken none. A scale whose ends nobody reaches is not a scale."""
    perfect = _facts(events_attended=10, no_shows=0, opportunities=10, last_attended_at=NOW)
    assert score_member(perfect, thresholds=DEFAULTS, now=NOW) == 100

    never = _facts(events_attended=0, no_shows=4, opportunities=10, last_attended_at=None)
    assert score_member(never, thresholds=DEFAULTS, now=NOW) == 0


def test_a_missing_part_is_left_out_rather_than_counted_as_zero() -> None:
    """A member with no reliability record is not thereby unreliable. Two members with
    identical attendance and recency, one of whom has also no-showed, must not score the
    same — and the one with no record must not be dragged down by a phantom zero."""
    clean = _facts(events_attended=5, no_shows=0, opportunities=10, last_attended_at=NOW)
    flaky = _facts(events_attended=5, no_shows=5, opportunities=10, last_attended_at=NOW)
    clean_score = score_member(clean, thresholds=DEFAULTS, now=NOW)
    flaky_score = score_member(flaky, thresholds=DEFAULTS, now=NOW)
    assert clean_score is not None and flaky_score is not None
    assert clean_score > flaky_score
    # commitment 0.5 + recency 1.0 + reliability 1.0 → 83, not (0.5 + 1.0 + 0.0)/3 → 50.
    assert clean_score == 83


@pytest.mark.parametrize(
    ("days_ago", "expected"),
    [(0, 1.0), (90, 1.0), (135, 0.5), (180, 0.0), (900, 0.0), (-5, 1.0)],
)
def test_recency_slides_from_the_window_to_twice_it(days_ago: int, expected: float) -> None:
    """Full marks inside the organizer's window, linear to zero at twice it, and never
    above full marks for an attendance dated in the future."""
    facts = _facts(last_attended_at=NOW - timedelta(days=days_ago))
    assert recency(facts, thresholds=Thresholds(lapsed_after_days=90), now=NOW) == pytest.approx(expected)


def test_recency_is_measured_from_attendance_not_from_contact() -> None:
    """A member who registers every month and never comes is recently absent, not
    recently engaged."""
    assert recency(_facts(last_attended_at=None, events_attended=0), thresholds=DEFAULTS, now=NOW) == 0.0


# --- end to end -----------------------------------------------------------------
def test_analyze_members_is_deterministic_and_covers_everyone() -> None:
    """The PRD's determinism criterion at the level the caller uses: same data, same
    answer, whatever order it arrives in."""
    records = [
        *(_a(f"e{i}", "champ", "attended") for i in range(10)),
        *(_a(f"e{i}", "ghost", "no_show") for i in range(4)),
        _a("e0", "once", "attended"),
        _a("e11", "newcomer", "attended"),
    ]
    first = analyze_members(EVENTS, records, now=NOW)
    assert [m.member_id for m in first] == ["champ", "ghost", "newcomer", "once"]
    assert first == analyze_members(EVENTS, list(reversed(records)), now=NOW)

    by_id = {m.member_id: m for m in first}
    assert by_id["champ"].lifecycle == CHAMPION
    assert by_id["ghost"].lifecycle == PROSPECT
    assert by_id["newcomer"].lifecycle == FIRST_TIMER
    assert by_id["once"].lifecycle == LAPSED  # attended the first event of twelve and never returned


def test_analyze_members_derives_the_lapse_guard_from_the_events() -> None:
    """The guard is derived from the calendar rather than asked of the caller, so a tool
    that forgets to pass it cannot silently lapse everyone. A member who attended the
    most recent event on a long-dead calendar has missed nothing."""
    old = [EventRecord(f"e{i}", f"E{i}", NOW - timedelta(days=400 + i)) for i in range(3)]
    assert analyze_members(old, [_a("e0", "m", "attended")], now=NOW)[0].lifecycle == FIRST_TIMER
    assert analyze_members(EVENTS, [_a("e0", "m", "attended")], now=NOW)[0].lifecycle == LAPSED


def test_a_quiet_recent_calendar_does_not_un_flag_someone_absent_for_years() -> None:
    """The whole reason this guard is shaped the way it is.

    The old test was ``any(event started in the last lapsed_after_days)`` — ONE boolean
    about the calendar, true or false for the entire roster at once. So an organizer
    whose last event was a couple of months ago un-flagged **everybody**, including a
    member who last came three years and fifty events ago. The label exists to surface
    exactly that person, and a recent quiet stretch is not evidence about them.

    Both members below are scored from the same calendar, in the same call, and must
    get different answers — which a single shared boolean cannot produce.
    """
    # Twelve events, the most recent 200 days back: nothing inside a 90-day window.
    calendar = [EventRecord(f"e{i}", f"E{i}", NOW - timedelta(days=200 + 30 * i)) for i in range(12)]
    thresholds = Thresholds(lapsed_after_days=90)
    records = [
        _a("e0", "long_gone", "attended"),  # e0 is the LATEST of the twelve
        _a("e11", "ancient", "attended"),  # e11 is the earliest — eleven events ago
    ]

    by_id = {s.member_id: s for s in analyze_members(calendar, records, now=NOW, thresholds=thresholds)}
    # Attended the most recent event there was: nothing has happened since, so nothing
    # was missed, however long ago it was.
    assert by_id["long_gone"].lifecycle != LAPSED
    # Absent while eleven events ran. The calendar being quiet lately says nothing
    # about them, and under the old guard it said everything.
    assert by_id["ancient"].lifecycle == LAPSED


def test_analyze_members_agrees_with_its_own_parts() -> None:
    """The composed call must not diverge from the functions it composes — otherwise the
    tests above are checking something the tool does not run."""
    thresholds = Thresholds(regular_min_events=3, champion_min_events=5, lapsed_after_days=45)
    records = [_a(f"e{i}", f"m{i % 4}", "attended") for i in range(12)]
    # `analyze_members` derives this from the calendar and hands it to every score, so a
    # bare `score_member` has to be given the same value or the two disagree by design
    # rather than by defect. Derived here the way the composed call derives it.
    last_settled = max(e.starts_at for e in EVENTS if e.over_by <= NOW)
    for scored in analyze_members(EVENTS, records, now=NOW, thresholds=thresholds):
        assert scored.score == score_member(
            scored.facts, thresholds=thresholds, now=NOW, last_settled_event_at=last_settled
        )
    assert [s.facts for s in analyze_members(EVENTS, records, now=NOW, thresholds=thresholds)] == member_facts(
        EVENTS, records, now=NOW
    )


# --- the thresholds object ------------------------------------------------------
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"regular_min_events": 1}, "at least 2"),
        ({"regular_min_events": 0}, "at least 2"),
        ({"regular_min_events": 5, "champion_min_events": 4}, "at least regular_min_events"),
        ({"champion_min_rate": 0.0}, r"\(0, 1\]"),
        ({"champion_min_rate": 1.5}, r"\(0, 1\]"),
        ({"lapsed_after_days": 0}, "at least 1"),
    ],
)
def test_an_unusable_threshold_set_is_rejected_at_construction(kwargs: dict[str, object], match: str) -> None:
    """Each of these makes a label unreachable or the ordering incoherent. Caught where
    the organizer set it, not several screens later as an empty group."""
    with pytest.raises(ValueError, match=match):
        Thresholds(**kwargs)  # type: ignore[arg-type]


def test_the_defaults_are_usable() -> None:
    """The no-argument path is the one most callers take; it must not be one of the
    combinations rejected above."""
    assert Thresholds() == Thresholds(
        regular_min_events=2, champion_min_events=4, champion_min_rate=0.5, lapsed_after_days=90
    )


# --- a threshold the app accepts must not break every later read ---


def test_a_lapsed_window_longer_than_a_timedelta_can_hold_is_refused_on_the_way_in() -> None:
    """`timedelta` tops out at 999,999,999 days, and `lifecycle_label` compares against
    one. With no upper bound, a value the settings screen accepted was persisted and then
    every analytics read of that store raised `OverflowError` — including `baraza report`,
    which does not catch it and printed a traceback. Refuse it where it arrives instead.
    """
    with pytest.raises(ValueError, match="lapsed_after_days"):
        Thresholds(lapsed_after_days=10**9)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"regular_min_events": 2.5},
        {"champion_min_events": 4.5},
        {"lapsed_after_days": 1.5},
    ],
)
def test_a_count_of_events_is_a_whole_number(kwargs: dict[str, object]) -> None:
    """These are counts compared against `len(...)`, and a float silently changed which
    side of a boundary a member fell on — 1.5 moved a member's score from 100 to 67. The
    store reconstructs `Thresholds` from JSON, so a hand-edited file reaches this
    constructor directly and the API's schema is not the only door.
    """
    with pytest.raises(ValueError):
        Thresholds(**kwargs)  # type: ignore[arg-type]


def test_a_quiet_calendar_does_not_decay_anybody() -> None:
    """An organizer who runs nothing for a season must not drag every member's score
    down. Recency measures to the last SETTLED event, not to the wall clock: with no
    events to miss, nobody has missed any.

    The lifecycle label already had this gate (`an_event_happened_while_absent`); the
    score did not, so the two disagreed — a member could hold a stable label while their
    number slid month after month, from nothing they did.
    """
    events = [EventRecord(f"e{i}", f"Event {i}", D(2026, 1, 1) + timedelta(days=30 * i)) for i in range(4)]
    records = [_a(f"e{i}", "m1", "attended") for i in range(4)]
    last_event = max(e.starts_at for e in events)

    day_after = analyze_members(events, records, now=last_event + timedelta(days=1))
    four_months_on = analyze_members(events, records, now=last_event + timedelta(days=120))

    assert [s.score for s in day_after] == [s.score for s in four_months_on], (
        "scores moved while the calendar was empty"
    )


def test_recency_still_decays_when_events_were_available_to_miss() -> None:
    """The other half, so the fix above cannot be satisfied by freezing recency outright.
    A member who stopped coming WHILE events kept running must still decay — that is the
    signal recency exists to carry.
    """
    events = [EventRecord(f"e{i}", f"Event {i}", D(2026, 1, 1) + timedelta(days=30 * i)) for i in range(8)]
    attended_early = [_a("e0", "m1", "attended")]
    attended_late = [_a("e0", "m2", "attended"), _a("e7", "m2", "attended")]

    now = D(2026, 1, 1) + timedelta(days=30 * 7 + 1)
    scored = {s.facts.member_id: s.score for s in analyze_members(events, attended_early + attended_late, now=now)}

    assert scored["m1"] is not None and scored["m2"] is not None
    assert scored["m1"] < scored["m2"], "someone who stopped coming must score below someone who kept coming"
