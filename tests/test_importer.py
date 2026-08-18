"""Tests for the folder import (``baraza.importer``).

The free-tier path: a folder of Luma guest-list exports, no API key. What is defended
here is mostly what happens when the folder is *not* tidy — a stray CSV, an event the
resolver cannot find, a guest with no email — because the organizer's downloads folder
is a real folder and none of those may cost them the import.

The resolver is injected as a fake throughout. It is a network call to an undocumented
endpoint; a suite that reached it would be testing Luma's uptime.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baraza.importer import import_folder, read_folder
from baraza.ingest import LumaEventMetadata, LumaResolveFailure, email_to_member_id
from baraza.store import StoredEvent, connect

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

HEADER = "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url"


def _csv(event_id: str, *rows: str) -> str:
    lines = [HEADER]
    for row in rows:
        lines.append(f"{row},https://lu.ma/check-in/{event_id}")
    return "\n".join(lines) + "\n"


def _folder(tmp_path: Path, files: dict[str, str]) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def _resolves(**named: str):  # noqa: ANN202
    """A fake resolver that knows the named events and nothing else."""

    def resolve(event_id: str):  # noqa: ANN202
        if event_id in named:
            return LumaEventMetadata(
                kind="resolved",
                luma_event_id=event_id,
                name=named[event_id],
                start_at=D(2026, 1, 10),
                end_at=D(2026, 1, 10, 2),
                timezone=None,
                cover_url=None,
            )
        return LumaResolveFailure(kind="unresolved", luma_event_id=event_id, reason="not_found", message="nope")

    return resolve


GUESTS = (
    "g1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,2026-01-10T18:00:00Z",
    "g2,Bo,bo@x.com,2026-01-02T00:00:00Z,approved,",
    "g3,Cy,cy@x.com,2026-01-03T00:00:00Z,invited,",
)


# --- reading the folder ------------------------------------------------------------
def test_a_file_that_is_not_a_luma_export_is_skipped_not_fatal(tmp_path: Path) -> None:
    """The folder an organizer points at is a real folder with other things in it. One
    stray CSV must not cost them the import."""
    folder = _folder(
        tmp_path,
        {
            "evt-a_-_Guests_-_2026-01-10-00-00-00.csv": _csv("evt-aaa", *GUESTS),
            "budget.csv": "month,spend\nJanuary,120\n",
            "notes.txt": "not a csv at all",
        },
    )
    parsed, skipped, warnings = read_folder(folder)
    assert [p.filename for p in parsed] == ["evt-a_-_Guests_-_2026-01-10-00-00-00.csv"]
    assert skipped == ["budget.csv"]
    assert any("budget.csv" in w for w in warnings)


def test_the_scan_is_not_recursive(tmp_path: Path) -> None:
    """"Point at your downloads" must not become an unbounded walk of everything the
    organizer has ever saved."""
    folder = _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)})
    nested = folder / "old"
    nested.mkdir()
    (nested / "b.csv").write_text(_csv("evt-bbb", *GUESTS), encoding="utf-8")

    parsed, _, _ = read_folder(folder)
    assert [p.filename for p in parsed] == ["a.csv"]


def test_a_missing_folder_is_an_error_with_the_path_in_it(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError, match="nowhere"):
        read_folder(tmp_path / "nowhere")


# --- the import ---------------------------------------------------------------------
def test_a_resolved_event_lands_with_its_guests(tmp_path: Path) -> None:
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(
        store,
        _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)}),
        now=NOW,
        resolve=_resolves(**{"evt-aaa": "January meetup"}),
    )

    assert (result.events, result.members, result.attendances) == (1, 3, 3)
    assert result.pending == ()
    (event,) = store.events()
    assert (event.event_id, event.title, event.starts_at) == ("evt-aaa", "January meetup", D(2026, 1, 10))
    assert {a.status for a in store.attendances()} == {"attended", "no_show", "invited"}
    store.close()


def test_the_derived_no_show_uses_the_event_not_the_clock(tmp_path: Path) -> None:
    """Bo was approved and never checked in. That is only a no-show because the event
    has ended — the same row before the event is still just a registration."""
    folder = _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)})
    resolve = _resolves(**{"evt-aaa": "January meetup"})

    bo = email_to_member_id("bo@x.com")

    after = connect(tmp_path / "after.db", create=True)
    import_folder(after, folder, now=NOW, resolve=resolve)
    assert {a.member_id: a.status for a in after.attendances()}[bo] == "no_show"
    after.close()

    before = connect(tmp_path / "before.db", create=True)
    import_folder(before, folder, now=D(2026, 1, 5), resolve=resolve)
    assert "no_show" not in {a.status for a in before.attendances()}
    before.close()


def test_a_guest_with_no_email_is_reported_rather_than_dropped_in_silence(tmp_path: Path) -> None:
    """Without an email there is no member id, so they cannot be joined to themselves
    across two exports — which is the one thing this tool promises. It still has to be
    visible that somebody was left out."""
    rows = (*GUESTS, "g4,Anon,,2026-01-04T00:00:00Z,approved,")
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(
        store,
        _folder(tmp_path, {"a.csv": _csv("evt-aaa", *rows)}),
        now=NOW,
        resolve=_resolves(**{"evt-aaa": "January meetup"}),
    )
    assert result.members == 3
    assert any("no email address" in w for w in result.warnings)
    store.close()


def test_the_same_person_across_two_exports_collapses_to_one_member(tmp_path: Path) -> None:
    """The acceptance criterion, end to end over the free path."""
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(
        store,
        _folder(
            tmp_path,
            {
                "a.csv": _csv("evt-aaa", "g1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,2026-01-10T18:00:00Z"),
                "b.csv": _csv("evt-bbb", "g9,Amy Smith,AMY@X.COM,2026-02-01T00:00:00Z,approved,"),
            },
        ),
        now=NOW,
        resolve=_resolves(**{"evt-aaa": "January", "evt-bbb": "February"}),
    )
    assert result.events == 2
    assert result.members == 1
    assert len(store.attendances()) == 2
    store.close()


def test_importing_the_same_folder_twice_changes_nothing(tmp_path: Path) -> None:
    """The refresh button. Every write underneath is an upsert on Luma's own grain, and
    this is the end-to-end proof that nothing between here and the file appends."""
    folder = _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)})
    resolve = _resolves(**{"evt-aaa": "January meetup"})
    store = connect(tmp_path / "b.db", create=True)

    first = import_folder(store, folder, now=NOW, resolve=resolve)
    snapshot = (store.events(), store.members(), store.attendances())
    second = import_folder(store, folder, now=NOW, resolve=resolve)

    assert (store.events(), store.members(), store.attendances()) == snapshot
    assert (first.events, first.members) == (second.events, second.members)
    store.close()


# --- the unresolved event -------------------------------------------------------------
def test_an_unresolvable_event_is_pending_not_an_error(tmp_path: Path) -> None:
    """Resolution failure falls back to manual entry, never an error. The
    guests are read and the CSVs stay in the folder, so nothing is lost."""
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(
        store,
        _folder(tmp_path, {"Secret_Party_-_Guests_-_2026-01-10-00-00-00.csv": _csv("evt-zzz", *GUESTS)}),
        now=NOW,
        resolve=_resolves(),
    )

    assert result.events == 0 and store.events() == []
    (pending,) = result.pending
    assert pending.luma_event_id == "evt-zzz"
    assert pending.guests == 3, "the guest count is how the organizer knows which event they are naming"
    assert pending.inferred_title == "Secret Party"
    assert pending.sources == ("Secret_Party_-_Guests_-_2026-01-10-00-00-00.csv",)
    assert pending.reason == "not_found"
    store.close()


def test_naming_a_pending_event_lets_the_next_import_finish_it(tmp_path: Path) -> None:
    """The manual-entry loop, whole. The organizer's date writes the event row; the
    next pass over the same folder finds it and attaches the guests. No re-upload."""
    folder = _folder(tmp_path, {"a.csv": _csv("evt-zzz", *GUESTS)})
    store = connect(tmp_path / "b.db", create=True)
    assert import_folder(store, folder, now=NOW, resolve=_resolves()).pending

    store.upsert_events([StoredEvent("evt-zzz", "Secret Party", D(2026, 3, 1))])
    again = import_folder(store, folder, now=NOW, resolve=_resolves())

    assert again.pending == ()
    assert again.attendances == 3
    assert store.events()[0].title == "Secret Party"
    store.close()


def test_the_pending_list_survives_the_pass_that_produced_it(tmp_path: Path) -> None:
    """The list is written to the store, not only returned. Before this, it existed
    only in one import's return value — so the Import screen could render it exactly
    once, and an organizer arriving from the CLI's "name them in the app" found a
    screen with no sign that work was waiting."""
    folder = _folder(tmp_path, {"Secret_Party_-_Guests_-_2026-01-10-00-00-00.csv": _csv("evt-zzz", *GUESTS)})
    store = connect(tmp_path / "b.db", create=True)
    import_folder(store, folder, now=NOW, resolve=_resolves())

    (entry,) = store.pending_events()
    assert entry["luma_event_id"] == "evt-zzz"
    assert entry["inferred_title"] == "Secret Party"
    assert entry["guests"] == 3
    assert store.last_import_folder() == str(folder.resolve())
    store.close()


def test_naming_an_event_retires_its_pending_entry_without_another_import(tmp_path: Path) -> None:
    """The read filters against the events table, so the entry disappears the moment
    the event exists — no cleanup pass to forget, no stale row to explain."""
    store = connect(tmp_path / "b.db", create=True)
    import_folder(store, _folder(tmp_path, {"a.csv": _csv("evt-zzz", *GUESTS)}), now=NOW, resolve=_resolves())
    assert store.pending_events()

    store.upsert_events([StoredEvent("evt-zzz", "Secret Party", D(2026, 3, 1))])
    assert store.pending_events() == []
    store.close()


def test_pending_entries_from_an_earlier_folder_survive_a_later_pass(tmp_path: Path) -> None:
    """Merged, not replaced: an organizer with two download folders must not lose the
    first folder's unnamed events by reading the second."""
    store = connect(tmp_path / "b.db", create=True)
    first = tmp_path / "one"
    first.mkdir()
    (first / "a.csv").write_text(_csv("evt-zzz", *GUESTS), encoding="utf-8")
    second = tmp_path / "two"
    second.mkdir()
    (second / "b.csv").write_text(_csv("evt-yyy", *GUESTS), encoding="utf-8")

    import_folder(store, first, now=NOW, resolve=_resolves())
    import_folder(store, second, now=NOW, resolve=_resolves())

    assert {entry["luma_event_id"] for entry in store.pending_events()} == {"evt-yyy", "evt-zzz"}
    assert store.last_import_folder() == str(second.resolve())
    store.close()


def test_the_store_is_consulted_before_the_resolver(tmp_path: Path) -> None:
    """Two reasons, and both matter. The organizer's typed date is authoritative and a
    later lookup must not overwrite it; and the ordinary refresh loop should make no
    calls at all to an undocumented endpoint for events already imported."""
    calls: list[str] = []

    def counting(event_id: str):  # noqa: ANN202
        calls.append(event_id)
        return _resolves(**{"evt-aaa": "What Luma calls it"})(event_id)

    folder = _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)})
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events([StoredEvent("evt-aaa", "What the organizer calls it", D(2026, 3, 1))])

    import_folder(store, folder, now=NOW, resolve=counting)
    assert calls == [], "the resolver was called for an event the store already had"
    assert store.events()[0].title == "What the organizer calls it"
    store.close()


def test_the_import_works_with_no_resolver_at_all(tmp_path: Path) -> None:
    """An organizer on a plane still gets their pending list rather than an exception."""
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, _folder(tmp_path, {"a.csv": _csv("evt-aaa", *GUESTS)}), now=NOW, resolve=None)
    assert result.pending[0].reason == "no_resolver"
    store.close()


def test_an_empty_folder_is_an_empty_result(tmp_path: Path) -> None:
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, _folder(tmp_path, {}), now=NOW, resolve=_resolves())
    assert (result.files_read, result.events, result.pending) == (0, 0, ())
    store.close()


# The phase's gate: importing the same corpus in ANY file order yields identical
# analytics, and a killed import leaves no partial state.

CHECKED_IN = "g1,Ada,ada@x.com,2026-05-01T09:00:00Z,approved,2026-05-20T18:04:00Z"
NO_CHECK_IN = "g2,Ada,ada@x.com,2026-05-02T09:00:00Z,approved,"


def test_the_answer_does_not_depend_on_what_the_files_are_called(tmp_path: Path) -> None:
    """rename an export and someone's attendance
    flips.

    One person, one event, two guest ids — they registered, cancelled, registered
    again, which is ordinary. The combiner keeps both, and correctly so: it dedupes on
    ``luma_guest_id`` and these genuinely are two guest records. They met instead in
    the store, whose attendances are keyed on ``(event_id, member_id)``, so the second
    write simply overwrote the first — and which one was second was the order the FILES
    were read in, which is alphabetical.
    """
    answers = []
    for index, (first_name, second_name) in enumerate((("a.csv", "b.csv"), ("b.csv", "a.csv"))):
        root = tmp_path / f"run{index}"
        root.mkdir()
        folder = _folder(root, {first_name: _csv("evt-A", CHECKED_IN), second_name: _csv("evt-A", NO_CHECK_IN)})
        store = connect(root / "b.db", create=True)
        import_folder(store, folder, now=NOW, resolve=_resolves(**{"evt-A": "May Meetup"}))
        answers.append([(a.member_id, a.status) for a in store.attendances()])
        store.close()

    assert answers[0] == answers[1], "the same two files gave different answers when renamed"
    # ...and the surviving answer is the one the evidence supports: they checked in.
    assert [status for _, status in answers[0]] == ["attended"]


def test_one_person_holds_one_registration_per_event(tmp_path: Path) -> None:
    """The property rather than the rename symptom: however many
    guest records one person has for one event, the store holds exactly one row for
    them. That is the grain analytics counts on — two rows would inflate turnout, and
    the wrong one of two is a wrong label on a real person."""
    folder = _folder(
        tmp_path,
        {
            "x.csv": _csv(
                "evt-A",
                "g1,Ada,ada@x.com,2026-05-01T09:00:00Z,approved,",
                "g2,Ada,ada@x.com,2026-05-02T09:00:00Z,approved,2026-05-20T18:04:00Z",
                "g3,Ada,ada@x.com,2026-05-03T09:00:00Z,cancelled,",
            )
        },
    )
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, folder, now=NOW, resolve=_resolves(**{"evt-A": "May Meetup"}))
    rows = store.attendances()
    store.close()

    assert len(rows) == 1
    # The check-in outranks both the cancellation and the later RSVP — of the three it
    # is the only one that is evidence of what actually happened.
    assert rows[0].status == "attended"
    assert result.attendances == 1  # the count reports what was written, not what was read


def test_a_folder_of_uppercase_exports_is_not_invisible(tmp_path: Path) -> None:
    """``glob("*.csv")`` is case-sensitive on macOS and Linux, so a folder
    holding ``EXPORT.CSV`` imported on Windows and came back empty on a Mac — with the
    file not even listed in ``files_skipped``, because a file the glob never matched is
    not a file the loop ever saw. "0 events", and nothing to go on."""
    folder = _folder(tmp_path, {"EXPORT.CSV": _csv("evt-A", CHECKED_IN)})
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, folder, now=NOW, resolve=_resolves(**{"evt-A": "May Meetup"}))
    events = store.events()
    store.close()

    assert len(events) == 1
    assert result.files_read == 1
    assert result.files_skipped == ()


def test_widening_the_match_did_not_start_reading_unrelated_files(tmp_path: Path) -> None:
    """The other side of it. Only the EXTENSION comparison relaxed — the folder
    an organizer points at is their real downloads folder, and the scan must not spread
    into it."""
    folder = _folder(tmp_path, {"notes.txt": "not a csv", "photo.CSVX": "nor this"})
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, folder, now=NOW, resolve=None)
    store.close()

    assert result.files_read == 0
    assert result.files_skipped == ()  # never opened, so never skipped either


def test_a_guest_whose_email_is_a_space_is_not_a_person(tmp_path: Path) -> None:
    """A blank-but-not-empty email must not reach ``email_to_member_id``,
    which strips it and hashes the empty string — so every such guest across every
    event collapses onto one member id and becomes a single fictional person, who is
    then scored and labelled like anyone real."""
    folder = _folder(
        tmp_path,
        {
            "x.csv": _csv(
                "evt-A",
                "g1,Ada,ada@x.com,2026-05-01T09:00:00Z,approved,",
                "g2,Nobody,   ,2026-05-02T09:00:00Z,approved,",
                "g3,Nobody Else,   ,2026-05-03T09:00:00Z,approved,",
            )
        },
    )
    store = connect(tmp_path / "b.db", create=True)
    result = import_folder(store, folder, now=NOW, resolve=_resolves(**{"evt-A": "May Meetup"}))
    members = store.members()
    store.close()

    assert [m.email for m in members] == ["ada@x.com"]
    assert email_to_member_id("") not in {m.member_id for m in members}
    # Both layers speak, each about its own decision, and they agree — the parser that
    # the row has no join key, the importer that it therefore wrote nothing. Each layer
    # states its own outcome, and the two must not contradict.
    assert sum("was not imported" in w for w in result.warnings) == 2
    assert sum("cannot be joined" in w for w in result.warnings) == 2


def test_a_killed_import_leaves_no_events_without_attendance(tmp_path: Path) -> None:
    """The second half of the gate.

    The three upserts used to be three transactions, so a crash between them committed
    events and people with no attendance against them — which analytics cannot tell
    apart from a real event nobody came to. The store reported a genuine 0% turnout for
    an import that never finished, and re-running fixed it, which is exactly why it was
    easy to miss: the wrong number was transient and plausible.

    Simulated by failing the LAST of the three writes, which is where the old shape was
    at its worst — everything before it had already committed.
    """
    folder = _folder(tmp_path, {"x.csv": _csv("evt-A", CHECKED_IN)})
    path = tmp_path / "b.db"
    store = connect(path, create=True)

    class _FailsOnTheLastWrite:
        """A connection that dies partway through. Wrapped rather than patched:
        ``sqlite3.Connection.executemany`` is read-only, and the transaction semantics
        under test are the real connection's, so everything else must pass through."""

        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self.writes = 0

        def executemany(self, sql: str, params: object) -> object:
            self.writes += 1
            if self.writes == 3:
                raise sqlite3.OperationalError("disk I/O error")
            return self._real.executemany(sql, params)

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

        def __enter__(self) -> object:
            return self._real.__enter__()

        def __exit__(self, *exc: object) -> object:
            return self._real.__exit__(*exc)

    real = store._db
    failing = _FailsOnTheLastWrite(real)
    store._db = failing  # type: ignore[assignment]
    with pytest.raises(sqlite3.OperationalError):
        import_folder(store, folder, now=NOW, resolve=_resolves(**{"evt-A": "May Meetup"}))
    store._db = real
    store.close()

    assert failing.writes == 3, "all three writes must have been attempted, or this proves nothing"
    store = connect(path)
    assert store.events() == []
    assert store.members() == []
    assert store.attendances() == []
    store.close()



# --- a name already known is never replaced by a guess -------------
#
# `_resolve_name` substituted the email's local part for a blank name before the store
# ever saw the row, so the store's "a blank never overwrites" rule (`_MEMBER_UPSERT`)
# had nothing to protect: every row arrived carrying a non-empty name. Re-exporting an
# event whose name column had been cleared replaced real names with `alice`-shaped
# stubs.


def _named(guest_id: str, name: str, email: str) -> str:
    return f"{guest_id},{name},{email},2026-01-01T00:00:00Z,approved,"


def _stored_member(store_path: Path, email: str):  # noqa: ANN202
    store = connect(store_path)
    try:
        member_id = email_to_member_id(email)
        return next(m for m in store.members() if m.member_id == member_id)
    finally:
        store.close()


def _stored_name(store_path: Path, email: str) -> str:
    return _stored_member(store_path, email).name


def test_a_later_export_with_a_blank_name_does_not_erase_the_name_we_had(tmp_path: Path) -> None:
    store_path = tmp_path / "s.db"
    named = _folder(tmp_path, {"a.csv": _csv("evt-1", _named("g1", "Xavier Xi", "xavier@x.com"))})
    import_folder(connect(store_path, create=True), named, now=NOW, resolve=_resolves(**{"evt-1": "E"}))
    assert _stored_name(store_path, "xavier@x.com") == "Xavier Xi"

    blank = tmp_path / "later"
    blank.mkdir()
    (blank / "a.csv").write_text(_csv("evt-1", _named("g1", "", "xavier@x.com")), encoding="utf-8")
    import_folder(connect(store_path), blank, now=NOW, resolve=_resolves(**{"evt-1": "E"}))

    assert _stored_name(store_path, "xavier@x.com") == "Xavier Xi"


def test_which_name_survives_does_not_depend_on_what_the_files_are_called(tmp_path: Path) -> None:
    """Two exports of one event disagreeing only on the name column. The tie-break used
    to fall through to `new.rsvp_at >= existing.rsvp_at`, which for equal RSVPs means the
    later-read file — so renaming a download decided the roster. Asserted as an equality
    between the two orderings rather than against one expected string, because a single
    expectation is re-baselined by whoever changes the ordering next.
    """
    rows = {"named": _named("g1", "Samantha Okonkwo", "sam@x.com"), "blank": _named("g1", "", "sam@x.com")}

    def run(first: str, second: str) -> str:
        folder = tmp_path / f"{first}-{second}"
        folder.mkdir()
        (folder / "1.csv").write_text(_csv("evt-1", rows[first]), encoding="utf-8")
        (folder / "2.csv").write_text(_csv("evt-1", rows[second]), encoding="utf-8")
        store_path = folder / "s.db"
        import_folder(connect(store_path, create=True), folder, now=NOW, resolve=_resolves(**{"evt-1": "E"}))
        return _stored_name(store_path, "sam@x.com")

    assert run("named", "blank") == run("blank", "named") == "Samantha Okonkwo"


def test_a_guest_the_export_never_named_still_shows_something_a_human_can_read(tmp_path: Path) -> None:
    """The fallback did not disappear, it moved to where it belongs: the store keeps what
    the source actually said, and the display derives a stand-in. Without this half, the
    fix above would trade a wrong name for an empty one."""
    folder = _folder(tmp_path, {"a.csv": _csv("evt-1", _named("g1", "", "quiet@x.com"))})
    store_path = tmp_path / "s.db"
    import_folder(connect(store_path, create=True), folder, now=NOW, resolve=_resolves(**{"evt-1": "E"}))

    member = _stored_member(store_path, "quiet@x.com")
    assert member.name == ""
    assert member.display_name == "quiet"
