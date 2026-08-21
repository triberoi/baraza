"""An event that recorded no attendance, across every number it touches.

The mode exists because two different facts arrive as identical rows: an event nobody
scanned, and an event nobody came to. Luma cannot tell them apart and neither can we, so
the organizer declares which — and the tests here are mostly about what the declaration
must NOT do. Counting a registration as an attendance is the easy half; withholding the
turnout figure is the half that makes the number safe to read.

The property under most of these: **the mode moves the people-level counts and the
event-level rate in opposite directions.** A test that only checked attendance went up
would pass against an implementation that also reported a 100% turnout, which is the one
outcome the feature exists to prevent.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from baraza.analytics import (
    CHECKED_IN,
    REGISTRATION_ONLY,
    AttendanceRecord,
    EventRecord,
    Thresholds,
    analyze_members,
    effective_status,
    event_attendance,
    member_facts,
    member_timeline,
    retention_grid,
)

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)


def _events(*evidence: str) -> list[EventRecord]:
    """One event a month, oldest first, each carrying the evidence given."""
    return [
        EventRecord(f"e{i}", f"Event {i}", NOW - timedelta(days=30 * (len(evidence) - i)), attendance_evidence=mode)
        for i, mode in enumerate(evidence)
    ]


def _a(event: str, member: str, status: str) -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


# --- the rule itself ------------------------------------------------------------
@pytest.mark.parametrize("status", ["waitlisted", "cancelled", "invited", "registered", "attended"])
def test_only_a_no_show_moves(status: str) -> None:
    """Nothing but the absent-but-committed guest is re-read.

    A waitlisted, cancelled or invited guest never said yes, so no reading of the evidence
    turns them into an attendee. An implementation that widened the rule to "everyone on
    the roster" would invent attendance for people who declined.
    """
    assert effective_status(status, REGISTRATION_ONLY) == status


def test_a_no_show_is_an_attendance_only_where_nothing_was_measured() -> None:
    assert effective_status("no_show", REGISTRATION_ONLY) == "attended"
    assert effective_status("no_show", CHECKED_IN) == "no_show"


def test_an_unrecognized_evidence_value_is_refused_rather_than_guessed() -> None:
    """Neither reading is safe, so there is no default to fall back on: one answer counts
    the room in and the other leaves it out, and silently picking either produces a number
    with no way to tell which it was."""
    events = [EventRecord("e0", "Event", NOW - timedelta(days=1), attendance_evidence="assumed")]
    with pytest.raises(ValueError, match="attendance_evidence"):
        member_facts(events, [_a("e0", "m", "no_show")], now=NOW)


# --- the two directions, together -----------------------------------------------
def test_the_room_counts_in_and_the_turnout_stays_out() -> None:
    """The whole feature in one pair of assertions, on one event.

    Three said yes, one was scanned, and the organizer says nobody was scanning. All three
    count as having attended — and the event reports NO turnout, because a rate over those
    three is 100% every time, for the one event nobody checked.
    """
    events = _events(REGISTRATION_ONLY)
    records = [_a("e0", "a", "attended"), _a("e0", "b", "no_show"), _a("e0", "c", "no_show")]

    (row,) = event_attendance(events, records, now=NOW)
    assert row.attended == 3
    assert row.no_shows == 0, "nobody was measured, so nobody was absent"
    assert row.show_rate is None, "a 100% here is the failure this mode exists to prevent"


def test_the_same_event_measured_reports_both() -> None:
    """The control for the test above: identical rows, `checked_in`, every number back.

    Without this pair the assertions above would also pass against an implementation that
    had simply stopped counting no-shows for everybody.
    """
    events = _events(CHECKED_IN)
    records = [_a("e0", "a", "attended"), _a("e0", "b", "no_show"), _a("e0", "c", "no_show")]

    (row,) = event_attendance(events, records, now=NOW)
    assert (row.attended, row.no_shows) == (1, 2)
    assert row.show_rate == pytest.approx(1 / 3)


def test_a_measured_event_beside_an_unmeasured_one_keeps_its_own_rate() -> None:
    """The mixed calendar, which is the case that made this worth building. Declaring one
    event unmeasured must not touch the other's rate — the reported harm was a fake 100%
    pulling the calendar figure up and making the scanned event look weak beside it."""
    events = _events(REGISTRATION_ONLY, CHECKED_IN)
    records = [
        _a("e0", "a", "no_show"),
        _a("e0", "b", "no_show"),
        _a("e1", "a", "attended"),
        _a("e1", "b", "no_show"),
    ]
    unmeasured, measured = event_attendance(events, records, now=NOW)
    assert unmeasured.show_rate is None
    assert measured.show_rate == pytest.approx(0.5)


def test_new_and_returning_still_partition_the_room() -> None:
    """The stacked bar has to keep adding up. An attendance that arrives through the
    evidence rather than a check-in must land in exactly one of the two segments, and the
    member's FIRST such attendance is what makes them new."""
    events = _events(REGISTRATION_ONLY, CHECKED_IN)
    records = [_a("e0", "m", "no_show"), _a("e1", "m", "attended")]

    first, second = event_attendance(events, records, now=NOW)
    assert (first.new_attendees, first.returning_attendees) == (1, 0)
    assert (second.new_attendees, second.returning_attendees) == (0, 1)


# --- people-level counts --------------------------------------------------------
def test_a_member_carries_how_many_of_their_attendances_were_unmeasured() -> None:
    """The count a screen needs to keep its promise. Two attendances is the total; that one
    of them is a registration is what somebody checking the number has to be able to see,
    and a total that cannot be broken down gives them nothing to check."""
    events = _events(REGISTRATION_ONLY, CHECKED_IN)
    records = [_a("e0", "m", "no_show"), _a("e1", "m", "attended")]

    (facts,) = member_facts(events, records, now=NOW)
    assert facts.events_attended == 2
    assert facts.unmeasured_attendances == 1
    assert facts.no_shows == 0


def test_a_measured_calendar_reports_no_unmeasured_attendances() -> None:
    events = _events(CHECKED_IN, CHECKED_IN)
    records = [_a("e0", "m", "attended"), _a("e1", "m", "attended")]
    (facts,) = member_facts(events, records, now=NOW)
    assert facts.unmeasured_attendances == 0


def test_the_timeline_agrees_with_the_roster_about_the_same_person() -> None:
    """The contradiction this must never produce: the roster counting someone as having
    attended while their own page says they said yes and did not come. One store cannot say
    both about one event, and the page somebody opens to check a number they distrust is
    the worst place to disagree."""
    events = _events(REGISTRATION_ONLY)
    records = [_a("e0", "m", "no_show")]

    (facts,) = member_facts(events, records, now=NOW)
    (entry,) = member_timeline("m", events, records)
    assert facts.events_attended == 1
    assert entry.status == "attended"
    assert entry.attendance_evidence == REGISTRATION_ONLY, "the row has to be markable as unmeasured"


def test_an_unmeasured_event_can_keep_a_member_out_of_lapsed() -> None:
    """The request, at the end where it is felt: somebody whose only recent event was an
    unscanned livestream is not lapsed. Asserted through `analyze_members` rather than the
    label function, because the wiring between the two is what would break."""
    events = _events(CHECKED_IN, REGISTRATION_ONLY)
    records = [_a("e0", "m", "attended"), _a("e1", "m", "no_show")]

    (scored,) = analyze_members(events, records, now=NOW, thresholds=Thresholds(lapsed_after_days=45))
    assert scored.lifecycle != "lapsed"
    assert scored.facts.events_attended == 2


def test_the_month_grid_reads_the_evidence_too() -> None:
    """Every view or none. A grid counting raw status would show a member returning at a
    measured event and vanishing at an unmeasured one, contradicting the roster."""
    events = _events(CHECKED_IN, REGISTRATION_ONLY)
    records = [_a("e0", "m", "attended"), _a("e1", "m", "no_show")]

    grid = retention_grid(events, records, now=NOW)
    (cohort,) = grid.cohorts
    assert cohort.size == 1
    assert cohort.attended[1] == 1, "their return landed on an unmeasured event and still counts"


# --- the mirror in the browser ---------------------------------------------------
VIZ_TS = Path(__file__).parents[1] / "ui" / "src" / "viz.ts"

#: `export const REGISTRATION_ONLY = 'registration_only'`, and its measured twin.
def _ts_constant(name: str, source: str) -> str | None:
    match = re.search(rf"export const {name}\s*=\s*'(?P<value>[^']*)'", source)
    return match.group("value") if match else None


@pytest.mark.parametrize(("name", "expected"), [("CHECKED_IN", CHECKED_IN), ("REGISTRATION_ONLY", REGISTRATION_ONLY)])
def test_the_browser_and_the_api_mean_the_same_words(name: str, expected: str) -> None:
    """The same shape ``test_cli.py`` holds ``percent`` to, for the same reason.

    The screens decide what to mark, and what to leave out of a rate, by comparing against
    these strings. Change one on a single side and nothing breaks loudly: the API keeps
    sending ``registration_only``, the UI keeps looking for something else, every event
    silently reads as measured, and the fake 100% comes back on the exact screen the
    feature exists to keep it off.
    """
    found = _ts_constant(name, VIZ_TS.read_text(encoding="utf-8"))
    assert found is not None, f"could not find {name} in {VIZ_TS.name} — did it move or change shape?"
    assert found == expected


@pytest.mark.parametrize("word", [CHECKED_IN, REGISTRATION_ONLY])
def test_the_browser_carries_no_second_copy_of_either_word(word: str) -> None:
    """One definition, imported everywhere.

    A component that inlined the string would keep working today and would NOT be caught
    by the test above, which reads only the exported constant — so the guard has to be
    that no second copy exists. It found one the day it was written (a `<option value>`
    on Settings, and a doc comment that quoted both), which is why it checks each quote
    style rather than the one that happened to be in use.
    """
    ui = VIZ_TS.parent
    strays = [
        path.relative_to(ui).as_posix()
        for path in ui.rglob("*.ts*")
        if path != VIZ_TS
        and any(quoted in path.read_text(encoding="utf-8") for quoted in (f"'{word}'", f'"{word}"', f"`{word}`"))
    ]
    assert not strays, f"these inline {word!r} instead of importing the constant: {strays}"
