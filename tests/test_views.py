"""Tests for the three roll-ups (``baraza.analytics.views``).

The recurring shape here is the same one the score sweep found: a view that reports
**zero** where it means **no data** draws a chart that lies. Each of the three is
tested for what it refuses to answer as much as for what it counts.
"""

from datetime import UTC, datetime, timedelta

import pytest

from baraza.analytics.members import AttendanceRecord, EventRecord
from baraza.analytics.scoring import LIFECYCLE_LABELS, analyze_members
from baraza.analytics.views import event_attendance, lifecycle_mix, member_timeline
from baraza.ingest import LumaGuest, resolve_guest_status

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

#: Six past events and two future ones, a month apart.
EVENTS = [EventRecord(f"e{i}", f"Event {i}", NOW - timedelta(days=30 * (6 - i))) for i in range(8)]
PAST = [e for e in EVENTS if e.starts_at <= NOW]


def _a(event: str, member: str, status: str) -> AttendanceRecord:
    return AttendanceRecord(event_id=event, member_id=member, status=status)


# --- per-event turnout ----------------------------------------------------------
def test_only_events_that_have_started_are_reported() -> None:
    """A future event has no turnout to split. Reporting it at zero attendance turns a
    healthy calendar into a cliff at the right-hand edge of every chart."""
    rows = event_attendance(EVENTS, [_a("e0", "m", "attended")], now=NOW)
    assert [r.event_id for r in rows] == [e.event_id for e in PAST]
    assert len(PAST) < len(EVENTS), "fixture must contain a future event for this to mean anything"


def test_rows_are_in_calendar_order() -> None:
    rows = event_attendance(EVENTS, [_a("e0", "m", "attended")], now=NOW)
    assert [r.starts_at for r in rows] == sorted(r.starts_at for r in rows)


def test_new_and_returning_partition_the_attendees() -> None:
    """Asserted as an invariant over a whole calendar rather than on one row: the split
    is what the overview chart stacks, so a member counted in neither (or in both) is a
    bar that does not add up to its own total."""
    records = [
        *(_a(f"e{i}", "regular", "attended") for i in range(6)),
        *(_a(f"e{i}", "joiner", "attended") for i in (2, 3)),
        _a("e5", "newcomer", "attended"),
        _a("e4", "ghost", "no_show"),
    ]
    for row in event_attendance(EVENTS, records, now=NOW):
        assert row.new_attendees + row.returning_attendees == row.attended


def test_new_means_their_first_ever_attendance() -> None:
    """Not their first appearance: someone who no-showed twice and then came is new the
    day they finally turn up, which is the moment the organizer actually converted them."""
    records = [_a("e0", "m", "no_show"), _a("e1", "m", "no_show"), _a("e2", "m", "attended"), _a("e3", "m", "attended")]
    rows = {r.event_id: r for r in event_attendance(EVENTS, records, now=NOW)}
    assert (rows["e0"].new_attendees, rows["e0"].no_shows) == (0, 1)
    assert (rows["e2"].new_attendees, rows["e2"].returning_attendees) == (1, 0)
    assert (rows["e3"].new_attendees, rows["e3"].returning_attendees) == (0, 1)


def test_every_attendee_is_new_exactly_once_across_the_calendar() -> None:
    """The property behind the row-level test: summing ``new_attendees`` gives the number
    of distinct people who have ever turned up. If a tie in ``starts_at`` moved the first
    attendance between two events, this would count someone twice or not at all."""
    same_instant = [EventRecord(f"t{i}", f"Tie {i}", NOW - timedelta(days=10)) for i in range(3)]
    records = [_a(f"t{i}", f"m{j}", "attended") for i in range(3) for j in range(4)]
    rows = event_attendance(same_instant, records, now=NOW)
    assert sum(r.new_attendees for r in rows) == 4


def test_the_roster_counts_everyone_and_committed_counts_the_yeses() -> None:
    """``committed`` is the only honest denominator for a show rate: a cancellation is
    not a broken promise, and counting it as one makes every event look worse."""
    records = [
        _a("e0", "came", "attended"),
        _a("e0", "missed", "no_show"),
        _a("e0", "bailed", "cancelled"),
        _a("e0", "queued", "waitlisted"),
    ]
    (row,) = [r for r in event_attendance(EVENTS, records, now=NOW) if r.event_id == "e0"]
    assert (row.roster, row.committed, row.attended, row.no_shows) == (4, 2, 1, 1)
    assert row.show_rate == 0.5


def test_the_show_rate_has_no_zero_denominator() -> None:
    """Every row is rendered, including an event only cancellations touched — and a row
    with nobody to count says so with ``None`` rather than reporting ``0.0``.

    The module's own rule, stated at the top of it: a count of zero and no data at all
    are different facts, and a chart cannot tell them apart. `0.0` here drew an event
    everybody cancelled as a total turnout collapse. The property this
    test is named for — no ZeroDivisionError — is unchanged.
    """
    (row,) = [r for r in event_attendance(EVENTS, [_a("e0", "m", "cancelled")], now=NOW) if r.event_id == "e0"]
    assert row.committed == 0 and row.show_rate is None


def test_a_turnout_of_zero_is_still_reported_as_zero() -> None:
    """The other side of the same rule, and the reason this cannot be `not committed`:
    an event people committed to and nobody attended IS a turnout collapse, and must
    keep saying so."""
    records = [_a("e0", "m1", "no_show"), _a("e0", "m2", "no_show")]
    (row,) = [r for r in event_attendance(EVENTS, records, now=NOW) if r.event_id == "e0"]
    assert row.committed == 2 and row.show_rate == 0.0


def test_an_event_nobody_touched_is_still_a_row() -> None:
    """Dropping empty events would silently close the gaps in a calendar, and the gaps
    are the signal — a series that stopped drawing people is exactly what to show."""
    rows = event_attendance(EVENTS, [_a("e0", "m", "attended")], now=NOW)
    assert all(r.roster == 0 for r in rows if r.event_id != "e0")


# --- lifecycle mix ---------------------------------------------------------------
def test_the_mix_is_zero_filled_in_label_order() -> None:
    """The mix is drawn as one ordinal bar. A label nobody currently holds must keep its
    place, or the ramp re-orders itself between runs as groups empty and refill."""
    members = analyze_members(EVENTS, [_a("e0", "m", "attended")], now=NOW)
    mix = lifecycle_mix(members)
    assert tuple(mix) == LIFECYCLE_LABELS
    assert sum(mix.values()) == len(members)
    assert sorted(mix.values())[:-1] == [0, 0, 0, 0]


def test_the_mix_totals_the_membership() -> None:
    records = [
        *(_a(f"e{i}", "champ", "attended") for i in range(6)),
        _a("e0", "once", "attended"),
        _a("e5", "recent", "attended"),
        _a("e4", "ghost", "no_show"),
    ]
    members = analyze_members(EVENTS, records, now=NOW)
    assert sum(lifecycle_mix(members).values()) == len(members) == 4


def test_an_empty_membership_still_returns_every_label() -> None:
    assert lifecycle_mix([]) == dict.fromkeys(LIFECYCLE_LABELS, 0)


# --- one member's history ---------------------------------------------------------
def test_the_timeline_includes_the_events_they_ignored() -> None:
    """The point of the view. A list of what someone attended cannot show a member going
    quiet; a list of everything on offer since they arrived can."""
    records = [_a("e1", "m", "attended"), _a("e3", "m", "no_show"), _a("e0", "other", "attended")]
    entries = member_timeline("m", EVENTS, records)
    assert [(e.event_id, e.status) for e in entries] == [
        ("e1", "attended"),
        ("e2", None),
        ("e3", "no_show"),
        ("e4", None),
        ("e5", None),
        ("e6", None),
        ("e7", None),
    ]


def test_the_timeline_starts_at_their_first_appearance() -> None:
    """Not at the start of the calendar. The events before someone existed are not part
    of their history and padding them in makes every newcomer look absent."""
    entries = member_timeline("m", EVENTS, [_a("e3", "m", "no_show")])
    assert entries[0].event_id == "e3"


def test_the_timeline_keeps_future_events() -> None:
    """Where the past/future split falls is the caller's call — and "they have not signed
    up for next month" is something the organizer wants to see."""
    entries = member_timeline("m", EVENTS, [_a("e0", "m", "attended")])
    assert [e.event_id for e in entries] == [e.event_id for e in EVENTS]
    assert entries[-1].starts_at > NOW


def test_an_unknown_member_is_rejected() -> None:
    """An empty timeline is indistinguishable from a mistyped id, and the tool takes ids
    from a hash of an email — a typo produces a plausible-looking one."""
    with pytest.raises(ValueError, match="no attendance records"):
        member_timeline("nobody", EVENTS, [_a("e0", "m", "attended")])


# --- shared validation ------------------------------------------------------------
@pytest.mark.parametrize(
    "call",
    [
        lambda events, records: event_attendance(events, records, now=NOW),
        lambda events, records: member_timeline("m", events, records),
    ],
    ids=["event_attendance", "member_timeline"],
)
def test_every_view_rejects_the_same_bad_record(call) -> None:  # noqa: ANN001
    """One validator behind all of them, so a bad record is refused the same way whichever
    view the caller reached for first — rather than raising in one and quietly producing a
    short answer in another."""
    with pytest.raises(ValueError, match="unknown status"):
        call(EVENTS, [_a("e0", "m", "showed_up")])
    with pytest.raises(ValueError, match="not in `events`"):
        call(EVENTS, [_a("nope", "m", "attended")])
    with pytest.raises(ValueError, match="timezone-aware"):
        call([EventRecord("e0", "E", datetime(2026, 1, 1))], [_a("e0", "m", "attended")])
    with pytest.raises(ValueError, match="appears twice in `events`"):
        call([EVENTS[0], EVENTS[0]], [_a("e0", "m", "attended")])
    with pytest.raises(ValueError, match="appears twice for event"):
        call(EVENTS, [_a("e0", "m", "attended"), _a("e0", "m", "no_show")])


def test_an_event_still_running_has_no_turnout_row_yet() -> None:
    """Not a rounding error — 100%, every time.

    A no-show is DERIVED, not recorded: Luma never sends one, so until an event ends
    nobody can be one. Publish the row the moment the event starts and ``no_shows`` is
    necessarily 0, ``committed`` necessarily equals ``attended``, and the show rate is
    1.0 by construction. That is not a measurement, it is the shape of the formula —
    and it aged into a real figure as the event ended, which is exactly what made it
    believable.
    """
    running = EventRecord("live", "Happening now", NOW - timedelta(hours=1), ends_at=NOW + timedelta(hours=2))
    records = [_a("live", "early_bird", "attended"), _a("live", "not_here_yet", "registered")]

    assert [row.event_id for row in event_attendance([running], records, now=NOW)] == []

    # ...and once it is over, the row appears and says something true.
    after = NOW + timedelta(hours=3)
    (row,) = event_attendance([running], records, now=after)
    assert (row.attended, row.committed, row.show_rate) == (1, 1, 1.0)


def test_an_event_with_no_recorded_end_still_appears_when_it_starts() -> None:
    """The limit, stated rather than papered over. A guest-list CSV carries no end time,
    so ``over_by`` falls back to the start — which is the same stand-in that decided the
    statuses being counted here. The two must agree: a turnout rate computed over
    statuses settled by a different cut-off than the rate itself is a number about
    nothing."""
    no_end = EventRecord("csv_only", "From a CSV", NOW - timedelta(hours=1))
    (row,) = event_attendance([no_end], [_a("csv_only", "m", "attended")], now=NOW)
    assert row.event_id == "csv_only"


def test_the_turnout_cut_matches_the_cut_that_decided_the_statuses() -> None:
    """The property: an event is countable here exactly when
    ``resolve_guest_status`` considers it over. Asserted against that function directly
    rather than against a copy of its rule, so the two cannot drift apart."""
    for ends_at in (None, NOW - timedelta(hours=1), NOW + timedelta(hours=2)):
        event = EventRecord("e", "E", NOW - timedelta(hours=3), ends_at=ends_at)
        rows = event_attendance([event], [_a("e", "m", "attended")], now=NOW)
        status_says_over = (
            resolve_guest_status(
                LumaGuest("g", "approved", "m@x", None, None, None),
                event_ended_at=event.over_by,
                now=NOW,
            )
            == "no_show"
        )
        assert bool(rows) == status_says_over, f"ends_at={ends_at!r}: the two cut-offs disagree"


# --- the application funnel -----------------------------------------------------
def _g(member: str, status: str, approval: str | None) -> AttendanceRecord:
    return AttendanceRecord(event_id="e0", member_id=member, status=status, luma_approval_status=approval)


def test_an_open_event_reports_no_funnel_rather_than_zeros() -> None:
    """Nothing in a Luma export says "this event required approval" — the only sign is
    the words themselves. An open event must therefore report ``None``, not three zeros:
    "0 applied" on an event nobody could apply to is a claim, and a false one.
    """
    records = [_g("m1", "attended", "approved"), _g("m2", "no_show", "approved"), _g("m3", "cancelled", "cancelled")]
    (row,) = event_attendance([EVENTS[0]], records, now=NOW)
    assert (row.applied, row.not_admitted, row.declined_or_withdrew) == (None, None, None)


def test_the_funnel_counts_the_people_who_asked_and_the_ones_left_undecided() -> None:
    """Six seats, ten apply, six approved, five turn up, one of the six declined.

    The numbers an organizer acts on: how many wanted in, and how many are still sitting
    in the queue. Neither may touch the show rate — the people who never got a place are
    not absentees.
    """
    records = [
        *[_g(f"in{i}", "attended", "approved") for i in range(5)],
        _g("in5", "no_show", "approved"),
        *[_g(f"waiting{i}", "waitlisted", "pending_approval") for i in range(2)],
        _g("asked", "waitlisted", "requested"),
        _g("turned_away", "cancelled", "declined"),
    ]
    (row,) = event_attendance([EVENTS[0]], records, now=NOW)

    assert row.applied == 10
    assert row.not_admitted == 3
    assert row.declined_or_withdrew == 1
    # The measured half is untouched by any of it.
    assert (row.attended, row.no_shows, row.committed) == (5, 1, 6)
    assert row.show_rate == 5 / 6


def test_a_host_invitation_is_not_an_application() -> None:
    """`invited` means the host reached out, which is the opposite direction of travel.
    Counting it as demand would inflate the number that exists to show demand."""
    records = [
        _g("applicant", "waitlisted", "pending_approval"),
        _g("guest", "attended", "invited"),
    ]
    (row,) = event_attendance([EVENTS[0]], records, now=NOW)
    assert row.applied == 1


def test_the_funnel_is_all_three_or_none_of_them() -> None:
    """A count of declines beside a blank total reads as "nobody applied and one was
    turned away" — a different claim, not a smaller one. Asserted as a property over the
    whole vocabulary rather than on the two cases above.
    """
    vocabulary = ("approved", "registered", "pending_approval", "requested", "invited", "waitlist", "declined", "")
    for word in vocabulary:
        for other in vocabulary:
            records = [_g("m1", "attended", word), _g("m2", "waitlisted", other)]
            (row,) = event_attendance([EVENTS[0]], records, now=NOW)
            present = {row.applied is None, row.not_admitted is None, row.declined_or_withdrew is None}
            assert len(present) == 1, f"{word!r}/{other!r} produced a partial funnel: {row}"


def test_the_parts_never_exceed_the_whole() -> None:
    """`not_admitted` and `declined_or_withdrew` are both subsets of `applied`, so an
    organizer can read them as a breakdown. A total smaller than its parts is the shape
    that makes a funnel chart draw backwards."""
    vocabulary = ("approved", "pending_approval", "requested", "invited", "waitlist", "declined")
    for i in range(len(vocabulary)):
        records = [_g(f"m{j}", "waitlisted", w) for j, w in enumerate(vocabulary[: i + 1])]
        (row,) = event_attendance([EVENTS[0]], records, now=NOW)
        if row.applied is None:
            continue
        assert row.not_admitted is not None and row.declined_or_withdrew is not None
        assert row.not_admitted + row.declined_or_withdrew <= row.applied
