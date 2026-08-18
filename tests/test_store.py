"""Tests for the SQLite artifact (``baraza.store``).

Three properties carry the weight, and each maps to a way an organizer's file goes
quietly wrong rather than loudly:

- **Re-import is idempotent.** The normal loop is "drop another export in the folder
  and press refresh". If that appended, every refresh would inflate the numbers.
- **A datetime survives the round trip as the same instant.** SQLite has no datetime
  type, so this is a convention, and a convention that loses the offset moves every
  event by hours without erroring anywhere.
- **A file from a newer baraza is refused.** Reading one would not raise — SQLite
  simply would not return the columns this version does not ask for — so the failure
  mode is a complete-looking answer computed from part of the data.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from baraza.analytics.scoring import analyze_members
from baraza.analytics.thresholds import Thresholds
from baraza.analytics.views import event_attendance
from baraza.store import (
    SCHEMA_VERSION,
    StoredAttendance,
    StoredEvent,
    StoredMember,
    StoreNotBaraza,
    StoreUnreadable,
    StoreVersionError,
    connect,
    open_store,
)

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731

EVENTS = [
    StoredEvent("e1", "January", D(2026, 1, 10), D(2026, 1, 10, 2), "The Hall", ("python", "beginners")),
    StoredEvent("e2", "February", D(2026, 2, 10)),
]
MEMBERS = [StoredMember("mem_0001", "amy@x.com", "Amy"), StoredMember("mem_0002", "bo@x.com", "Bo")]
ATTENDANCES = [
    StoredAttendance("e1", "mem_0001", "attended", D(2026, 1, 1), D(2026, 1, 10)),
    StoredAttendance("e1", "mem_0002", "no_show", D(2026, 1, 2)),
    StoredAttendance("e2", "mem_0001", "attended", D(2026, 2, 1), D(2026, 2, 10)),
]


def _loaded(path: Path):  # noqa: ANN202 — the Store type is the module's, not the test's business
    store = connect(path, create=True)
    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    store.upsert_attendances(ATTENDANCES)
    return store


# --- round trip -----------------------------------------------------------------
def test_everything_written_reads_back_unchanged(tmp_path: Path) -> None:
    """Including the fields analytics never sees — venue and tags are their own screens,
    and a store that dropped them would make those screens impossible without a re-import."""
    store = _loaded(tmp_path / "b.db")
    assert store.events() == EVENTS
    assert store.members() == MEMBERS
    assert store.attendances() == ATTENDANCES
    store.close()


def test_a_timestamp_survives_as_the_same_instant(tmp_path: Path) -> None:
    """Written in one offset, read back in UTC, still the same moment. The convention is
    text, so nothing here would error if it were wrong — it would just be a few hours out."""
    tokyo = timezone(timedelta(hours=9))
    written = datetime(2026, 3, 1, 9, 30, tzinfo=tokyo)
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events([StoredEvent("e", "Tokyo meetup", written)])

    (read_back,) = store.events()
    assert read_back.starts_at == written
    assert read_back.starts_at.tzinfo is not None
    store.close()


def test_a_naive_datetime_is_refused_at_the_door(tmp_path: Path) -> None:
    """It would read back as a different instant, silently. Refused on write rather than
    on read: the file outlives the process, and by read time nobody knows what was meant."""
    store = connect(tmp_path / "b.db", create=True)
    with pytest.raises(ValueError, match="naive datetime"):
        store.upsert_events([StoredEvent("e", "E", datetime(2026, 3, 1, 9, 30))])
    store.close()


def test_an_optional_timestamp_stays_none(tmp_path: Path) -> None:
    """``ends_at`` and ``checked_in_at`` are genuinely absent for many rows — a no-show has
    no check-in — so ``None`` must not round-trip into an epoch or a string."""
    store = _loaded(tmp_path / "b.db")
    assert store.events()[1].ends_at is None
    assert [a for a in store.attendances() if a.status == "no_show"][0].checked_in_at is None
    store.close()


def test_an_empty_store_reads_as_empty_not_as_an_error(tmp_path: Path) -> None:
    store = connect(tmp_path / "b.db", create=True)
    assert (store.events(), store.members(), store.attendances()) == ([], [], [])
    assert store.calendar() == ([], [])
    store.close()


# --- idempotence ------------------------------------------------------------------
def test_importing_the_same_export_twice_changes_nothing(tmp_path: Path) -> None:
    """The property the organizer's refresh button rests on."""
    path = tmp_path / "b.db"
    store = _loaded(path)
    once = (store.events(), store.members(), store.attendances())

    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    store.upsert_attendances(ATTENDANCES)
    assert (store.events(), store.members(), store.attendances()) == once
    store.close()


def test_a_re_import_refreshes_a_changed_row_rather_than_duplicating_it(tmp_path: Path) -> None:
    """An event renamed on Luma, a guest who upgraded from no-show to attended after a
    late check-in was synced. One row each, carrying the newer fact."""
    store = _loaded(tmp_path / "b.db")
    store.upsert_events([StoredEvent("e1", "January (renamed)", D(2026, 1, 10), D(2026, 1, 10, 2), "The Hall")])
    store.upsert_attendances([StoredAttendance("e1", "mem_0002", "attended", D(2026, 1, 2), D(2026, 1, 10, 1))])

    events = store.events()
    assert len(events) == len(EVENTS)
    assert events[0].title == "January (renamed)"
    # The tags SURVIVE. This line used to assert `== ()` with the note "the update is a
    # replacement, not a merge" — the rule stated as an expectation: the
    # caller above passes no tags, and a source with nothing to say about a field must
    # not erase what another source filled in. See the precedence rule on `Store`.
    assert events[0].tags == ("python", "beginners")
    assert events[0].venue == "The Hall"  # ...and a real value still replaces the old one

    attendances = store.attendances()
    assert len(attendances) == len(ATTENDANCES)
    assert [a.status for a in attendances if a.member_id == "mem_0002"] == ["attended"]
    store.close()


def test_a_stale_re_import_never_un_attends_a_checked_in_guest(tmp_path: Path) -> None:
    """Importing a pre-event export (no check-in) after a post-event one must not null
    the check-in or flip the guest back to no_show. Luma never un-checks a guest, so a
    recorded check-in — and its 'attended' status — is permanent across imports. Events and
    members already merged present-over-absent; attendances did not, and silently corrupted
    the store on the ordinary re-import-a-different-folder path."""
    store = _loaded(tmp_path / "b.db")  # mem_0001 attended e1 with a check-in on Jan 10
    store.upsert_attendances([StoredAttendance("e1", "mem_0001", "no_show", D(2026, 1, 1))])
    row = next(a for a in store.attendances() if a.event_id == "e1" and a.member_id == "mem_0001")
    assert row.status == "attended"
    assert row.checked_in_at == D(2026, 1, 10)
    store.close()


def test_a_re_import_keeps_the_later_rsvp_whichever_order_it_arrives(tmp_path: Path) -> None:
    """The other half of the merge rule: the later RSVP wins and an earlier one arriving
    afterwards does not clobber it."""
    store = _loaded(tmp_path / "b.db")  # mem_0002 no_show, rsvp Jan 2
    store.upsert_attendances([StoredAttendance("e1", "mem_0002", "no_show", D(2026, 1, 5))])
    store.upsert_attendances([StoredAttendance("e1", "mem_0002", "no_show", D(2026, 1, 3))])
    row = next(a for a in store.attendances() if a.event_id == "e1" and a.member_id == "mem_0002")
    assert row.rsvp_at == D(2026, 1, 5)
    store.close()


@pytest.mark.parametrize("filename", ["a space.db", "pct%20name.db", "hash#tag.db"])
def test_a_store_named_with_uri_special_chars_opens_the_right_file(tmp_path: Path, filename: str) -> None:
    """A read-only open builds a ``file:`` URI. Unencoded, ``%`` re-decoded the path to
    a different file (``pct%20name.db`` → ``pct name.db``) and ``#`` split ``?mode=ro`` off as
    a fragment — losing read-only and opening a created stray file. The literal filename must
    round-trip through the read-only open."""
    path = tmp_path / filename
    writer = connect(path, create=True)
    writer.upsert_events(EVENTS)
    writer.upsert_members(MEMBERS)
    writer.upsert_attendances(ATTENDANCES)
    writer.close()

    reader = connect(path, read_only=True)
    try:
        assert [e.event_id for e in reader.events()] == ["e1", "e2"]
    finally:
        reader.close()


def test_the_same_person_in_two_exports_stays_one_member(tmp_path: Path) -> None:
    """The acceptance criterion, at the level the store is responsible for: the id
    is the key, so a second export carrying the same person updates rather than adds."""
    store = _loaded(tmp_path / "b.db")
    store.upsert_members([StoredMember("mem_0001", "amy@x.com", "Amy Smith")])
    assert [m.member_id for m in store.members()] == ["mem_0001", "mem_0002"]
    assert store.members()[0].name == "Amy Smith"
    store.close()


# --- what the store refuses --------------------------------------------------------
def test_an_unknown_status_is_refused_on_write(tmp_path: Path) -> None:
    """A bad value written now is a wrong number in every future run. Analytics would
    reject it on read, but by then it is in the organizer's file."""
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    with pytest.raises(ValueError, match="unknown status"):
        store.upsert_attendances([StoredAttendance("e1", "mem_0001", "showed_up")])
    store.close()


def test_pointing_the_store_at_a_directory_is_a_sentence_not_a_traceback(tmp_path: Path) -> None:
    """`--store` at a folder must raise a sentence, not a raw `sqlite3.OperationalError` naming neither the
    path nor the cause, and the web app 500'd on it. It is now a `StoreError` with the path."""
    a_directory = tmp_path / "somedir"
    a_directory.mkdir()
    with pytest.raises(StoreUnreadable, match=str(a_directory.name)):
        connect(a_directory)


def test_pointing_the_store_at_a_non_database_file_is_a_sentence(tmp_path: Path) -> None:
    """A text file or image opens but is not a SQLite database. Raw, it escapes as
    `file is not a database`. `StoreNotBaraza` already promised to cover this case."""
    not_a_db = tmp_path / "notes.txt"
    not_a_db.write_text("hello, I am plain text, not a baraza store", encoding="utf-8")
    with pytest.raises(StoreNotBaraza, match="not a readable SQLite database"):
        connect(not_a_db)


def test_creating_a_store_under_a_missing_directory_is_a_sentence(tmp_path: Path) -> None:
    """The first import into a path whose parent does not exist must not raise a raw
    `unable to open database file`."""
    with pytest.raises(StoreUnreadable, match="parent directory"):
        connect(tmp_path / "nope" / "b.db", create=True)


def test_an_attendance_for_an_unknown_event_or_member_is_refused(tmp_path: Path) -> None:
    """Foreign keys are ON. Without them SQLite accepts the orphan happily and the row is
    invisible to every join afterwards."""
    import sqlite3

    store = _loaded(tmp_path / "b.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_attendances([StoredAttendance("no-such-event", "mem_0001", "attended")])
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_attendances([StoredAttendance("e1", "no-such-member", "attended")])
    store.close()


def test_a_file_from_a_newer_baraza_is_refused(tmp_path: Path) -> None:
    """The quiet failure this guard exists for: today's queries against tomorrow's file do
    not raise, they just do not ask for the columns they do not know about — so the
    organizer gets a complete-looking answer computed from part of their data."""
    import sqlite3

    path = tmp_path / "b.db"
    connect(path, create=True).close()
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(StoreVersionError, match="newer baraza"):
        connect(path, create=True)


def test_a_file_from_this_version_reopens(tmp_path: Path) -> None:
    """The other half: the guard must not reject the file it just wrote. A version check
    that fails closed on its own output would be discovered by the first user, not here."""
    path = tmp_path / "b.db"
    store = _loaded(path)
    store.close()

    reopened = connect(path, create=True)
    assert reopened.events() == EVENTS
    reopened.close()


# --- the organizer's settings ----------------------------------------------------------
def test_thresholds_default_until_someone_sets_them(tmp_path: Path) -> None:
    store = connect(tmp_path / "b.db", create=True)
    assert store.thresholds() == Thresholds()
    store.close()


def test_thresholds_survive_a_reopen(tmp_path: Path) -> None:
    """They are a property of *this* data, which is what makes the single-file promise
    hold — copy the file to another machine and the roster reads the same."""
    path = tmp_path / "b.db"
    chosen = Thresholds(regular_min_events=3, champion_min_events=8, champion_min_rate=0.75, lapsed_after_days=45)
    store = connect(path, create=True)
    store.set_thresholds(chosen)
    store.close()

    reopened = connect(path, create=True)
    assert reopened.thresholds() == chosen
    reopened.close()


def test_setting_thresholds_twice_replaces_rather_than_accumulates(tmp_path: Path) -> None:
    store = connect(tmp_path / "b.db", create=True)
    store.set_thresholds(Thresholds(lapsed_after_days=30))
    store.set_thresholds(Thresholds(lapsed_after_days=60))
    assert store.thresholds().lapsed_after_days == 60
    store.close()


def test_thresholds_a_file_cannot_satisfy_are_an_error_not_a_shrug(tmp_path: Path) -> None:
    """Hand-edited, or written by a version whose rules were looser. Falling back to the
    defaults would silently re-label the whole roster — the organizer would see different
    people in different groups and nothing would say why."""
    import sqlite3

    path = tmp_path / "b.db"
    connect(path, create=True).close()
    with sqlite3.connect(path) as raw:
        raw.execute("INSERT INTO settings (key, value) VALUES ('thresholds', ?)", ('{"regular_min_events": 1}',))

    store = connect(path, create=True)
    with pytest.raises(ValueError, match="not usable"):
        store.thresholds()
    store.close()


# --- the pending list -------------------------------------------------------------------
def test_pending_events_round_trip_and_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "b.db"
    entry = {
        "luma_event_id": "evt-a",
        "inferred_title": "Book Club",
        "guests": 3,
        "sources": ["a.csv"],
        "reason": "not_found",
    }
    store = connect(path, create=True)
    store.set_pending_events([entry])
    store.set_last_import_folder("C:\\exports")
    store.close()

    reopened = connect(path, create=True)
    assert reopened.pending_events() == [entry]
    assert reopened.last_import_folder() == "C:\\exports"
    reopened.close()


def test_a_pending_entry_whose_event_now_exists_is_filtered_on_read(tmp_path: Path) -> None:
    """Naming an event is what retires its entry — by construction, not by cleanup."""
    store = connect(tmp_path / "b.db", create=True)
    store.set_pending_events(
        [
            {"luma_event_id": "evt-a", "guests": 1},
            {"luma_event_id": "evt-b", "guests": 2},
        ]
    )
    store.upsert_events([StoredEvent("evt-a", "Book Club", datetime(2026, 3, 1, tzinfo=UTC))])
    assert [entry["luma_event_id"] for entry in store.pending_events()] == ["evt-b"]
    store.close()


def test_a_mangled_pending_row_reads_as_absent_not_as_an_error(tmp_path: Path) -> None:
    """Unlike the thresholds row, this is a convenience cache over facts the folder
    still holds — refusing to open the store over it would make the cache load-bearing,
    which is exactly what it must not be."""
    import sqlite3

    path = tmp_path / "b.db"
    connect(path, create=True).close()
    with sqlite3.connect(path) as raw:
        raw.execute("INSERT INTO settings (key, value) VALUES ('pending_events', 'not json at all')")

    store = connect(path, create=True)
    assert store.pending_events() == []
    store.close()


def test_the_api_key_is_not_in_the_store(tmp_path: Path) -> None:
    """Stated as a check because the file gets copied around. Settings that describe the
    data belong in it; a credential does not, and it lives on disk beside the store
    instead."""
    import sqlite3

    path = tmp_path / "b.db"
    store = connect(path, create=True)
    store.set_thresholds(Thresholds())
    store.close()
    with sqlite3.connect(path) as raw:
        assert [row[0] for row in raw.execute("SELECT key FROM settings")] == ["thresholds"]


# --- the schema version -------------------------------------------------------------------
def test_a_v1_file_upgrades_in_place_and_keeps_its_data(tmp_path: Path) -> None:
    """v1 had no ``settings`` table; v2 added it. The upgrade is `CREATE TABLE IF NOT EXISTS`
    plus a version stamp, which works because the change is additive — the module docstring
    on SCHEMA_VERSION says what has to happen the first time one is not."""
    import sqlite3

    path = tmp_path / "b.db"
    store = _loaded(path)
    store.close()
    with sqlite3.connect(path) as raw:  # pretend it was written by the version before this one
        raw.execute("DROP TABLE settings")
        raw.execute("PRAGMA user_version = 1")

    upgraded = connect(path, create=True)
    assert upgraded.events() == EVENTS
    assert upgraded.attendances() == ATTENDANCES
    assert upgraded.thresholds() == Thresholds()
    upgraded.close()

    with sqlite3.connect(path) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


# --- the seam onto analytics --------------------------------------------------------
def test_the_calendar_projection_is_all_analytics_can_see(tmp_path: Path) -> None:
    """Scoring gets an id, a title, a start, an end and a status — never an email and
    never a venue, because neither is an input to a score. Asserted on the shape so that
    a field added to the store later does not quietly widen what scoring reads.

    ``ends_at`` was added deliberately: whether an event is over
    decides whether its turnout is a measurement or an artifact of the formula, so it IS
    an input to a number. This test going red was the widening being noticed, which is
    what it is for — the field set is re-pinned rather than the assertion loosened."""
    store = _loaded(tmp_path / "b.db")
    events, attendances = store.calendar()

    assert [vars(e) for e in events] == [
        {"event_id": "e1", "title": "January", "starts_at": D(2026, 1, 10), "ends_at": D(2026, 1, 10, 2)},
        {"event_id": "e2", "title": "February", "starts_at": D(2026, 2, 10), "ends_at": None},
    ]
    # Re-pinned, not loosened. `luma_approval_status` is carried for the application
    # funnel and is NOT an input to any number — the test below holds it to that, which
    # is the condition on which this set was allowed to grow.
    assert set(vars(attendances[0])) == {"event_id", "member_id", "status", "luma_approval_status"}
    # The two the projection exists to withhold, named so the guard cannot be satisfied
    # by a field set that happens to have grown one of them.
    assert not {"venue", "tags", "email"} & set(vars(events[0]))
    store.close()


def test_the_raw_approval_word_cannot_change_a_single_number(tmp_path: Path) -> None:
    """The condition on which the projection above was allowed to grow a field.

    `luma_approval_status` carries the application funnel, which the six-value `status`
    cannot: an applicant nobody admitted and a real waitlist are both `waitlisted`. The
    three funnel counts ARE read from it — that is its whole purpose — and nothing else
    may be. Every score, every label, and every MEASURED turnout figure must stay
    byte-identical whatever the word says, including words Luma has never emitted.

    So the funnel fields are excluded by name rather than the comparison being loosened.
    If a fourth reporting-only field is added later it has to be named here too, which is
    the point: widening this set is a decision someone has to take on purpose.
    """
    funnel = {"applied", "not_admitted", "declined_or_withdrew"}

    def measured(row: object) -> dict[str, object]:
        return {k: v for k, v in vars(row).items() if k not in funnel}

    store = _loaded(tmp_path / "b.db")
    events, attendances = store.calendar()
    baseline_scores = [vars(m) for m in analyze_members(events, attendances, now=D(2026, 6, 1))]
    baseline_turnout = [measured(r) for r in event_attendance(events, attendances, now=D(2026, 6, 1))]

    for word in ("approved", "pending_approval", "requested", "waitlist", "declined", "", "INVENTED_LATER", None):
        altered = [replace(a, luma_approval_status=word) for a in attendances]
        assert [vars(m) for m in analyze_members(events, altered, now=D(2026, 6, 1))] == baseline_scores, (
            f"{word!r} moved a score or a label"
        )
        assert [measured(r) for r in event_attendance(events, altered, now=D(2026, 6, 1))] == baseline_turnout, (
            f"{word!r} moved a measured turnout figure"
        )
    store.close()


def test_a_stored_calendar_scores_the_same_as_one_held_in_memory(tmp_path: Path) -> None:
    """The round trip must not change an answer. Persisting is not meant to be part of the
    part of how a number is computed, and this is what says so."""
    now = D(2026, 3, 1)
    store = _loaded(tmp_path / "b.db")
    events, attendances = store.calendar()
    store.close()

    from_store = analyze_members(events, attendances, now=now)
    assert [(m.member_id, m.lifecycle, m.score) for m in from_store] == [
        ("mem_0001", "regular", 100),
        ("mem_0002", "prospect", 0),
    ]


def test_open_store_closes_the_file(tmp_path: Path) -> None:
    """The context manager is how the CLI and the server should both take it — and on
    Windows an unclosed handle is not a leak, it is a file nobody else can open."""
    import sqlite3

    path = tmp_path / "b.db"
    with open_store(path, create=True) as store:
        store.upsert_events(EVENTS)
    with pytest.raises(sqlite3.ProgrammingError):
        store.events()


# --- precedence ----------------------------------------------------------------------
def test_a_source_with_nothing_to_say_about_a_field_leaves_it_alone(tmp_path: Path) -> None:
    """At the grain it happens.

    The folder route builds its ``StoredEvent`` from a guest-list CSV, which carries no
    venue and no tags, so it passes the dataclass defaults — and the upsert wrote those
    over whatever an earlier API import had filled in. Two screens' worth of what a
    paying organizer pays for, erased by a refresh that reported success.
    """
    store = _loaded(tmp_path / "b.db")
    before = store.events()[0]
    assert before.venue and before.tags, "the fixture has to start with both, or this proves nothing"

    # Exactly what the folder route sends: an id, a title and dates. Nothing else.
    store.upsert_events([StoredEvent(before.event_id, before.title, before.starts_at, before.ends_at)])

    after = store.events()[0]
    assert after.venue == before.venue
    assert after.tags == before.tags
    assert after.ends_at == before.ends_at
    store.close()


def test_a_real_value_still_replaces_an_older_one(tmp_path: Path) -> None:
    """The rule is "absent loses", not "first wins" — the API route is still supposed to
    overwrite a stored title and venue when Luma has a better answer. A guard that
    froze the first value would break the paid route to fix the free one."""
    store = _loaded(tmp_path / "b.db")
    before = store.events()[0]
    store.upsert_events(
        [
            StoredEvent(
                before.event_id,
                "Renamed on Luma",
                before.starts_at,
                before.ends_at,
                venue="A Different Hall",
                tags=("rust",),
            )
        ]
    )

    after = store.events()[0]
    assert (after.title, after.venue, after.tags) == ("Renamed on Luma", "A Different Hall", ("rust",))
    store.close()


def test_a_blank_name_does_not_erase_a_real_one(tmp_path: Path) -> None:
    """The same rule on members, which is the half the store owns: one
    route sends a derived name, the other used to send ``""``, and whichever ran last
    won. Normalizing the two routes stops them disagreeing; this stops a disagreement
    destroying the better answer."""
    store = _loaded(tmp_path / "b.db")
    member = store.members()[0]
    assert member.name, "the fixture has to start with a name"

    store.upsert_members([StoredMember(member.member_id, "", "")])

    after = next(m for m in store.members() if m.member_id == member.member_id)
    assert (after.name, after.email) == (member.name, member.email)
    store.close()


def test_the_three_writes_of_one_import_are_one_transaction(tmp_path: Path) -> None:
    """The reason :meth:`Store.write_import` exists rather than three
    calls at the call site.

    The old shape is still reachable — the three ``upsert_*`` methods are public — so
    this asserts the difference rather than only the fix: calling them separately
    commits as it goes, and a failure partway leaves events with no attendance against
    them. Analytics cannot tell that apart from a real event nobody came to.
    """
    store = connect(tmp_path / "b.db", create=True)

    # The old shape: separate calls, so the first is already committed when the second
    # is rejected. `upsert_attendances` validates before writing, so a bad status is a
    # clean way to make the second call fail.
    store.upsert_events(EVENTS)
    with pytest.raises(ValueError):
        store.upsert_attendances([StoredAttendance(EVENTS[0].event_id, "mem_0001", "not-a-status", D(2026, 1, 2))])
    assert store.events() != [], "the separate-call shape commits the events regardless — that is the defect"

    # The new shape: same failure, nothing committed.
    store2 = connect(tmp_path / "c.db", create=True)
    with pytest.raises(ValueError):
        store2.write_import(
            EVENTS,
            MEMBERS,
            [StoredAttendance(EVENTS[0].event_id, "mem_0001", "not-a-status", D(2026, 1, 2))],
        )
    assert store2.events() == []
    assert store2.members() == []
    store2.close()
    store.close()
