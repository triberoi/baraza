"""How the artifact is opened — create, read, repair — as distinct from what it holds.

The properties an organizer feels:

- A mistyped path is an error, not a new empty store answering "nothing imported yet".
- A read is a read: no write lock, no migration, no file created.
- Somebody else's database is refused rather than adopted, and the refusal leaves no open
  handle — on Windows that is also a file they can no longer delete.
- A file short of the schema is repaired even when it claims to be current.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baraza.analytics.thresholds import Thresholds
from baraza.store import (
    SCHEMA_VERSION,
    StoredAttendance,
    StoredEvent,
    StoredMember,
    StoreNeedsUpgrade,
    StoreNotBaraza,
    StoreNotFound,
    connect,
)

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731

EVENTS = [StoredEvent("e1", "January", D(2026, 1, 10), D(2026, 1, 10, 2), "The Hall", ("python",))]
MEMBERS = [StoredMember("mem_0001", "amy@x.com", "Amy")]
ATTENDANCES = [StoredAttendance("e1", "mem_0001", "attended", D(2026, 1, 1), D(2026, 1, 10))]


def _loaded(path: Path) -> None:
    store = connect(path, create=True)
    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    store.upsert_attendances(ATTENDANCES)
    store.close()


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as raw:
        return {row[1] for row in raw.execute(f"PRAGMA table_info({table})")}


# --- creating is something a caller asks for ---------------------------------------------
def test_opening_a_store_that_is_not_there_is_an_error_not_a_new_empty_one(tmp_path: Path) -> None:
    """A mistyped ``--store`` used to be indistinguishable from a first run: it created an
    empty file and answered "nothing imported yet", which is a confident wrong answer."""
    missing = tmp_path / "typo.db"
    with pytest.raises(StoreNotFound, match="no baraza store"):
        connect(missing)
    assert not missing.exists()


def test_a_read_only_open_never_brings_a_file_into_existence(tmp_path: Path) -> None:
    """The MCP surface's failure: a model asking about a typo'd path was told
    "your community is empty" and a new empty store was left behind at that path."""
    missing = tmp_path / "typo.db"
    with pytest.raises(StoreNotFound):
        connect(missing, read_only=True)
    assert not missing.exists()


def test_asking_to_create_a_read_only_store_is_refused_as_incoherent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="contradictory"):
        connect(tmp_path / "x.db", create=True, read_only=True)


# --- a read is a read ---------------------------------------------------------------------
def test_a_read_only_open_does_not_write_a_single_byte(tmp_path: Path) -> None:
    """Opening used to rewrite the file's structure every time, which is why loading any
    screen during an import stalled behind a write lock, and why the "read-only" MCP surface
    wrote to the organizer's store on every question."""
    path = tmp_path / "b.db"
    _loaded(path)
    before = path.read_bytes()

    store = connect(path, read_only=True)
    assert store.events() == EVENTS  # a real read, not merely an open
    store.close()

    assert path.read_bytes() == before


def test_a_read_only_open_cannot_write_even_when_asked(tmp_path: Path) -> None:
    """The mode is SQLite's own, not a convention this module has to remember to honor."""
    path = tmp_path / "b.db"
    _loaded(path)

    store = connect(path, read_only=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        store.set_thresholds(Thresholds(lapsed_after_days=99))
    store.close()


def test_a_writable_open_of_a_current_file_also_writes_nothing(tmp_path: Path) -> None:
    """The write lock was taken on every open, not only on reads. A file that is already
    converged has nothing to converge, so nothing should be written."""
    path = tmp_path / "b.db"
    _loaded(path)
    before = path.read_bytes()

    connect(path).close()

    assert path.read_bytes() == before


# --- somebody else's file ------------------------------------------------------------------
def test_a_database_that_is_not_ours_is_refused_rather_than_written_into(tmp_path: Path) -> None:
    """Pointing --store at another SQLite file used to adopt it: our tables appeared inside
    it and it was stamped with our schema version."""
    foreign = tmp_path / "someone-elses.db"
    with sqlite3.connect(foreign) as raw:
        raw.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        raw.execute("INSERT INTO notes (body) VALUES ('not yours')")

    with pytest.raises(StoreNotBaraza, match="not a baraza one"):
        connect(foreign)

    with sqlite3.connect(foreign) as raw:
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        stamp = raw.execute("PRAGMA user_version").fetchone()[0]
    assert tables == {"notes"}, "our schema was written into a database we do not own"
    assert stamp == 0, "someone else's file was stamped with our version"


def test_a_file_that_is_not_a_database_is_refused_without_leaking_a_handle(tmp_path: Path) -> None:
    """The leak is the other half of it, and on Windows it is the half a user sees:
    a handle left open is also a file they can no longer delete or move."""
    document = tmp_path / "notes.txt"
    document.write_text("Dear diary, today I mistyped --store.\n", encoding="utf-8")

    # A sentence, never a raw `sqlite3.DatabaseError`: `StoreNotBaraza`
    # already promised to cover "not a database at all", and only now delivers it.
    with pytest.raises(StoreNotBaraza, match="not a readable SQLite database"):
        connect(document)

    document.unlink()  # PermissionError here on Windows if the connection leaked
    assert not document.exists()


# --- repair, measured against the shape rather than the stamp --------------------------------
def test_a_file_missing_a_column_is_repaired_even_though_it_claims_to_be_current(tmp_path: Path) -> None:
    """The sticky-stamp bug, and the reason convergence is measured against the
    schema's shape instead of its version number.

    The old upgrade ran ``CREATE TABLE IF NOT EXISTS`` — which cannot add a column — and then
    stamped the current version regardless. Files went out marked up to date while missing a
    column, and once marked, a corrected version could no longer tell them from a good file.
    So the repair has to fire on a file that says it is already current.
    """
    path = tmp_path / "b.db"
    _loaded(path)
    with sqlite3.connect(path) as raw:
        raw.execute("ALTER TABLE events DROP COLUMN venue")
        raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    assert "venue" not in _columns(path, "events")

    connect(path).close()

    assert "venue" in _columns(path, "events")


def test_a_repaired_file_is_not_migrated_again_on_every_later_open(tmp_path: Path) -> None:
    """Convergence compares column NAMES, not ``table_info`` rows. A column added by ALTER
    lands at a different ordinal than the same column created by the schema, so comparing
    rows would report a repaired file as still short and rewrite it on every open — which is
    the write lock this phase just removed, reintroduced by the repair."""
    path = tmp_path / "b.db"
    _loaded(path)
    with sqlite3.connect(path) as raw:
        raw.execute("ALTER TABLE events DROP COLUMN venue")

    connect(path).close()
    settled = path.read_bytes()
    connect(path).close()

    assert path.read_bytes() == settled


def test_repairing_a_missing_column_keeps_the_rows_that_were_already_there(tmp_path: Path) -> None:
    """An upgrade that loses data is not an upgrade."""
    path = tmp_path / "b.db"
    _loaded(path)
    with sqlite3.connect(path) as raw:
        raw.execute("ALTER TABLE events DROP COLUMN venue")

    store = connect(path)
    (january,) = store.events()
    store.close()

    assert (january.event_id, january.title) == ("e1", "January")
    assert january.venue == ""  # the column is back, at its declared default


def test_a_read_only_open_refuses_an_older_file_rather_than_answering_from_it(tmp_path: Path) -> None:
    """A read must not migrate. It must also not answer from a file it knows is short of the
    schema, because that answer would be wrong rather than absent."""
    path = tmp_path / "b.db"
    _loaded(path)
    with sqlite3.connect(path) as raw:
        raw.execute("ALTER TABLE events DROP COLUMN venue")
        raw.execute("PRAGMA user_version = 1")

    with pytest.raises(StoreNeedsUpgrade, match="older baraza"):
        connect(path, read_only=True)

    connect(path).close()  # a writable open is what upgrades it
    connect(path, read_only=True).close()


# --- timestamps read the same everywhere ------------------------------------------------------
def test_a_stored_timestamp_with_no_offset_reads_as_utc_on_any_host(tmp_path: Path) -> None:
    """``_to_text`` refuses to write one, so a naive value in the file came from somewhere
    else — a hand edit, another tool. Reading it with ``astimezone`` assumed the **host's**
    zone, so the same file gave different dates on different machines. UTC is a
    guess; it is the same guess everywhere, which is the property that was missing.
    """
    path = tmp_path / "b.db"
    _loaded(path)
    with sqlite3.connect(path) as raw:
        raw.execute("UPDATE events SET starts_at = '2026-01-10T00:00:00' WHERE event_id = 'e1'")

    store = connect(path, read_only=True)
    (january,) = store.events()
    store.close()

    assert january.starts_at == datetime(2026, 1, 10, tzinfo=UTC)
