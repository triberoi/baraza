"""The local SQLite artifact — one file, beside the organizer's exports.

Facts only: what a Luma export said. Scores and labels are derived on read, so changing
a threshold re-labels the roster immediately with no cache to invalidate.

Re-import is an upsert, not an append — importing the same file twice must leave the
store as importing it once did.

Timestamps are ISO-8601 UTC text, which sorts lexicographically in chronological order so
an index works, and comes back timezone-aware.

PRIVACY: this file holds attendees' names and email addresses, at the default permissions
of the account that runs the tool, unencrypted. Organizers copy it around.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from baraza.analytics.members import AttendanceRecord, EventRecord
from baraza.analytics.thresholds import Thresholds
from baraza.ingest import GUEST_STATUSES

#: Bumped whenever the schema below changes shape. Stored in ``PRAGMA user_version``,
#: which SQLite carries in the file header for exactly this purpose — no table needed
#: and no chance of the marker being missing from a file that has one.
#:
#: An older file converges on this schema when opened for writing, at table AND column
#: grain, measured against the shape :data:`_SCHEMA` produces.
#:
#: NOT handled, and needing a real migration step rather than a version bump: dropping a
#: column, narrowing a type, rewriting rows, or adding a ``NOT NULL`` column with no
#: default — :func:`_add_column_sql` raises rather than half-applying one.
SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id  TEXT PRIMARY KEY,
    title     TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at   TEXT,
    venue     TEXT NOT NULL DEFAULT '',
    tags      TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS members (
    member_id TEXT PRIMARY KEY,
    email     TEXT NOT NULL,
    name      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendances (
    event_id     TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    member_id    TEXT NOT NULL REFERENCES members(member_id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    rsvp_at      TEXT,
    checked_in_at TEXT,
    -- Luma's own word, beside the status it was translated into. The translation is lossy
    -- both ways: an unadmitted applicant and a real waitlist both become `waitlisted`; a
    -- decline, a withdrawal and a cancellation all become `cancelled`. Reported, never
    -- measured — it carries the application funnel without a seventh published status.
    luma_approval_status TEXT,
    PRIMARY KEY (event_id, member_id)
);
CREATE INDEX IF NOT EXISTS attendances_by_member ON attendances (member_id);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: The organizer's threshold settings, as one JSON row in ``settings``. Kept here and
#: not in a config file beside the store because they are a property of *this* data —
#: copy the file to another machine and the roster reads the same, which is the whole
#: promise of a single-file artifact. The Luma API key is the opposite case and
#: deliberately does NOT live here: it is a credential, and this file gets copied.
_THRESHOLDS_KEY = "thresholds"

#: The pending list and the folder it came from, as JSON rows in ``settings`` — the
#: same home as the thresholds, and for the same reason: they are a property of this
#: data. Neither is a schema change, so an older baraza opening this file simply never
#: asks for the keys.
_PENDING_EVENTS_KEY = "pending_events"
_LAST_FOLDER_KEY = "last_import_folder"


#: The tables that make a file ours. Checked before anything is written, so an unrelated
#: SQLite database is refused rather than quietly gaining a `members` table.
_OUR_TABLES = frozenset({"events", "members", "attendances"})


class StoreError(Exception):
    """Anything wrong with the file itself, as opposed to what is in it.

    One base so a caller — the CLI in particular — can turn every one of these into a
    sentence with a single ``except``. Each subclass says something different to an
    organizer, and none of them should ever arrive as a traceback.
    """


class StoreVersionError(StoreError):
    """The file was written by a newer baraza than the one reading it."""


class StoreNotFound(StoreError):
    """There is no store at that path, and the caller did not ask to create one."""


class StoreNotBaraza(StoreError):
    """A database, but not ours — or not a database at all."""


class StoreUnreadable(StoreError):
    """The path cannot be opened as a SQLite database at all — it is a directory, its
    parent directory does not exist, or the process cannot open it. Distinct from
    :class:`StoreNotBaraza` (a file that opened but holds the wrong thing)."""


class StoreNeedsUpgrade(StoreError):
    """An older file, reached by a path that must not migrate it."""


@dataclass(frozen=True)
class StoredEvent:
    """An event as the store keeps it — more than:class:`EventRecord` carries,
    because the venue and tag cuts are screens of their own and the facts have to
    survive the round trip to reach them."""

    event_id: str
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    venue: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StoredMember:
    """A person. ``member_id`` is ``baraza.ingest.email_to_member_id``'s output and is
    computed by the caller, not here — the store persists identity, it does not
    decide it."""

    member_id: str
    email: str
    #: What a source actually called this person, or ``""`` when none of them did.
    #: Empty is a fact — "nobody told us" — and is what lets the upsert keep a real
    #: name when a later export arrives with the column blank. For rendering, use
    #::attr:`display_name`; storing the stand-in here is what broke that.
    name: str

    @property
    def display_name(self) -> str:
        """A name to put on a screen, derived rather than stored.

        The email's local part is a reasonable stand-in and a poor fact: written into
        the store it is indistinguishable from a name a source supplied, and the next
        import cannot tell it is safe to replace.
        """
        if self.name.strip():
            return self.name
        local = self.email.split("@", 1)[0]
        return local or "(unknown guest)"


@dataclass(frozen=True)
class StoredAttendance:
    """One member's record for one event, with the two Luma timestamps the derived
    no-show was computed from. Keeping them means the store can be re-scored later
    without going back to the CSVs."""

    event_id: str
    member_id: str
    status: str
    rsvp_at: datetime | None = None
    checked_in_at: datetime | None = None
    #: Luma's own approval word, unmapped. ``None`` for a row written by a caller that
    #: does not have one; nothing is measured from it.
    luma_approval_status: str | None = None


def _to_text(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ValueError(f"refusing to store a naive datetime ({moment!r}) — it would read back as a different instant")
    return moment.astimezone(UTC).isoformat()


def _naive_is_utc(moment: datetime) -> datetime:
    """Read a stored timestamp as UTC when it carries no offset.

    ``_to_text`` refuses to write a naive datetime, so one in the file was put there by
    something else — a hand edit, another tool. ``.astimezone(UTC)`` on a naive value assumes
    the **host's** zone, which is why the same file read the same way gave different dates on
    different machines. UTC is a guess, but it is the same guess
    everywhere, and every timestamp this module writes is UTC.
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None else moment


def _from_text(text: str | None) -> datetime | None:
    return None if text is None else _naive_is_utc(datetime.fromisoformat(text)).astimezone(UTC)


def _required(text: str | None) -> datetime:
    """A NOT NULL timestamp column. Reading NULL out of one means the file was written
    by something other than this module, so it is an error rather than a ``None``."""
    if text is None:
        raise ValueError("a NOT NULL timestamp column read back as NULL — the file is corrupt")
    return _naive_is_utc(datetime.fromisoformat(text)).astimezone(UTC)


class Store:
    """An open SQLite artifact. Prefer:func:`open_store`, which closes it for you."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._db = connection

    # --- writes -----------------------------------------------------------------
    #
    # Precedence, decided once: an absent value never replaces a present one. A blank is
    # not a disagreement, it is the absence of a claim. A guest-list CSV carries no venue
    # and no tags, so the folder route passes `""` and `()` — which would otherwise
    # overwrite what an earlier API import filled in, and report success.
    #
    # The rule belongs to the STORE: the collision happens here in `ON CONFLICT`, so fixing
    # it in a caller would fix one route and leave every future caller to remember it.
    _EVENT_UPSERT = """
        INSERT INTO events (event_id, title, starts_at, ends_at, venue, tags)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            title      = CASE WHEN excluded.title != ''  THEN excluded.title ELSE events.title END,
            starts_at  = excluded.starts_at,
            ends_at    = COALESCE(excluded.ends_at, events.ends_at),
            venue      = CASE WHEN excluded.venue != ''  THEN excluded.venue ELSE events.venue END,
            tags       = CASE WHEN excluded.tags != '[]' THEN excluded.tags  ELSE events.tags  END
    """
    # `starts_at` is NOT NULL, so it has no absent case to guard. `ends_at` is nullable and
    # the guest-list CSV never has one, hence COALESCE rather than a string comparison.

    _MEMBER_UPSERT = """
        INSERT INTO members (member_id, email, name) VALUES (?, ?, ?)
        ON CONFLICT(member_id) DO UPDATE SET
            email = CASE WHEN excluded.email != '' THEN excluded.email ELSE members.email END,
            name  = CASE WHEN excluded.name  != '' THEN excluded.name  ELSE members.name  END
    """

    @staticmethod
    def _event_params(events: list[StoredEvent]) -> list[tuple[Any, ...]]:
        return [
            (e.event_id, e.title, _to_text(e.starts_at), _to_text(e.ends_at), e.venue, json.dumps(list(e.tags)))
            for e in events
        ]

    def upsert_events(self, events: list[StoredEvent]) -> None:
        """Insert or refresh. Re-importing an export whose title or venue changed on
        Luma updates the row rather than adding a second one — but a source with
        nothing to say about a field leaves what is already there alone."""
        with self._db:
            self._db.executemany(self._EVENT_UPSERT, self._event_params(events))

    def upsert_members(self, members: list[StoredMember]) -> None:
        """As :meth:`upsert_events`: a better value replaces a worse one, a blank replaces
        nothing. The two routes disagree about the same person's name and email, so
        without this, switching routes flips their details back and forth."""
        with self._db:
            self._db.executemany(self._MEMBER_UPSERT, [(m.member_id, m.email, m.name) for m in members])

    # A re-import must not un-attend a guest: importing a pre-event export after a
    # post-event one would otherwise null the check-in and flip the guest to no_show.
    # :func:`_supersedes` rule 1 applied across imports — Luma never un-checks a guest, so a
    # recorded check-in is permanent and its status stays with it; the later RSVP wins.
    # Timestamps are one canonical UTC text form, so a lexical `>` compares chronologically.
    _ATTENDANCE_UPSERT = """
        INSERT INTO attendances (event_id, member_id, status, rsvp_at, checked_in_at, luma_approval_status)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id, member_id) DO UPDATE SET
            status = CASE
                WHEN excluded.checked_in_at IS NULL AND attendances.checked_in_at IS NOT NULL
                THEN attendances.status ELSE excluded.status END,
            -- Follows `status` exactly: the two describe the same export, so the row that
            -- keeps its status keeps the word that status was translated from.
            luma_approval_status = CASE
                WHEN excluded.checked_in_at IS NULL AND attendances.checked_in_at IS NOT NULL
                THEN attendances.luma_approval_status ELSE excluded.luma_approval_status END,
            checked_in_at = COALESCE(excluded.checked_in_at, attendances.checked_in_at),
            rsvp_at = CASE
                WHEN excluded.rsvp_at IS NULL THEN attendances.rsvp_at
                WHEN attendances.rsvp_at IS NULL THEN excluded.rsvp_at
                WHEN excluded.rsvp_at > attendances.rsvp_at THEN excluded.rsvp_at
                ELSE attendances.rsvp_at END
    """

    @staticmethod
    def _checked_attendance_params(attendances: list[StoredAttendance]) -> list[tuple[Any, ...]]:
        """Validate, then shape. Rejects a status outside
        :data:`baraza.ingest.GUEST_STATUSES` before any row is written — the file outlives
        the process, so a bad value is a wrong number in every future run."""
        for record in attendances:
            if record.status not in GUEST_STATUSES:
                raise ValueError(f"unknown status {record.status!r} — expected one of {sorted(GUEST_STATUSES)}")
        return [
            (
                a.event_id,
                a.member_id,
                a.status,
                _to_text(a.rsvp_at),
                _to_text(a.checked_in_at),
                a.luma_approval_status,
            )
            for a in attendances
        ]

    def upsert_attendances(self, attendances: list[StoredAttendance]) -> None:
        """Keyed on ``(event_id, member_id)`` — Luma's own grain, and the reason a
        re-import cannot double-count a guest."""
        params = self._checked_attendance_params(attendances)
        with self._db:
            self._db.executemany(self._ATTENDANCE_UPSERT, params)

    def write_import(
        self,
        events: list[StoredEvent],
        members: list[StoredMember],
        attendances: list[StoredAttendance],
    ) -> None:
        """Write one import's three row sets in ONE transaction — all of it, or none.

        Three separate transactions let a crash commit events and people with no
        attendance against them, which is indistinguishable from an event nobody came to:
        a plausible 0% turnout that a re-run quietly fixes.

        Validation runs before the transaction opens, so a bad status raises without
        SQLite starting work it would have to roll back.
        """
        event_params = self._event_params(events)
        member_params = [(m.member_id, m.email, m.name) for m in members]
        attendance_params = self._checked_attendance_params(attendances)
        with self._db:
            self._db.executemany(self._EVENT_UPSERT, event_params)
            self._db.executemany(self._MEMBER_UPSERT, member_params)
            self._db.executemany(self._ATTENDANCE_UPSERT, attendance_params)

    # --- reads ------------------------------------------------------------------
    def events(self) -> list[StoredEvent]:
        """Every event, in calendar order."""
        rows = self._db.execute(
            "SELECT event_id, title, starts_at, ends_at, venue, tags FROM events ORDER BY starts_at, event_id"
        ).fetchall()
        return [
            StoredEvent(
                event_id=row[0],
                title=row[1],
                starts_at=_required(row[2]),
                ends_at=_from_text(row[3]),
                venue=row[4],
                tags=tuple(json.loads(row[5])),
            )
            for row in rows
        ]

    def members(self) -> list[StoredMember]:
        rows = self._db.execute("SELECT member_id, email, name FROM members ORDER BY member_id").fetchall()
        return [StoredMember(*row) for row in rows]

    def attendances(self) -> list[StoredAttendance]:
        rows = self._db.execute(
            """SELECT event_id, member_id, status, rsvp_at, checked_in_at, luma_approval_status
               FROM attendances ORDER BY event_id, member_id"""
        ).fetchall()
        return [
            StoredAttendance(
                event_id=row[0],
                member_id=row[1],
                status=row[2],
                rsvp_at=_from_text(row[3]),
                checked_in_at=_from_text(row[4]),
                luma_approval_status=row[5],
            )
            for row in rows
        ]

    # --- the organizer's settings --------------------------------------------------
    def thresholds(self) -> Thresholds:
        """The organizer's boundaries, or the defaults if they never set any.

        A stored set that no longer validates — written by a version whose rules were
        looser, or edited by hand in a SQLite browser — raises rather than silently
        falling back to the defaults. A roster that quietly re-labels itself because a
        setting was dropped is the worst of both.
        """
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (_THRESHOLDS_KEY,)).fetchone()
        if row is None:
            return Thresholds()
        try:
            return Thresholds(**json.loads(row[0]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the thresholds stored in this file are not usable: {exc}") from exc

    def set_thresholds(self, thresholds: Thresholds) -> None:
        """Replace them. Validated by:class:`Thresholds` itself before it gets here —
        an unusable set never reaches the file."""
        with self._db:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_THRESHOLDS_KEY, json.dumps(asdict(thresholds))),
            )

    # --- what the last folder read could not name ----------------------------------
    def pending_events(self) -> list[dict[str, Any]]:
        """Events whose guests were read but which still need a name and a date.

        Written by the importer at the end of every folder pass, so the list survives
        the pass that produced it — before this, it existed only in one import's return
        value, and the Import screen could show it exactly once. An entry whose event
        has since been named (its id is in ``events``) is filtered out here rather than
        depended on being cleaned up, so naming an event retires it by construction.

        A row that does not parse is treated as absent, not as an error: this is a
        convenience cache over facts the folder still holds, never the facts themselves.
        """
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (_PENDING_EVENTS_KEY,)).fetchone()
        if row is None:
            return []
        try:
            entries = json.loads(row[0])
        except ValueError:
            return []
        if not isinstance(entries, list):
            return []
        known = {event.event_id for event in self.events()}
        return [
            entry
            for entry in entries
            if isinstance(entry, dict) and entry.get("luma_event_id") not in known
        ]

    def set_pending_events(self, entries: list[dict[str, Any]]) -> None:
        """Replace the pending list. The importer merges; this only writes."""
        with self._db:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_PENDING_EVENTS_KEY, json.dumps(entries)),
            )

    def last_import_folder(self) -> str | None:
        """The folder the last import read, so the Import screen can offer it back.

        A path, not a credential — but it is a path on the ORGANIZER'S machine, which is
        why it lives in the store beside their data and is never echoed anywhere else.
        """
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (_LAST_FOLDER_KEY,)).fetchone()
        return row[0] if row is not None else None

    def set_last_import_folder(self, folder: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_LAST_FOLDER_KEY, folder),
            )

    # --- the seam onto analytics -------------------------------------------------
    def calendar(self) -> tuple[list[EventRecord], list[AttendanceRecord]]:
        """The pair every :mod:`baraza.analytics` entry point takes.

        The projection is the point: analytics never sees an email or a venue, because
        neither is an input to a score. ``ends_at`` is carried, because whether an event is
        over decides whether its turnout is a measurement or an artifact of the formula.
        """
        events = [
            EventRecord(event_id=e.event_id, title=e.title, starts_at=e.starts_at, ends_at=e.ends_at)
            for e in self.events()
        ]
        attendances = [
            AttendanceRecord(
                event_id=a.event_id,
                member_id=a.member_id,
                status=a.status,
                luma_approval_status=a.luma_approval_status,
            )
            for a in self.attendances()
        ]
        return events, attendances

    def close(self) -> None:
        self._db.close()


def _shape(connection: sqlite3.Connection) -> dict[str, dict[str, tuple[Any, ...]]]:
    """``{table: {column: PRAGMA table_info row}}`` for everything but SQLite's own."""
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    # The table name is interpolated because PRAGMA takes no bound parameters — verified
    # 2026-08-14, `PRAGMA table_info(?)` is a syntax error, so this is forced rather than
    # lazy. The name comes from sqlite_master in this same file, never from a caller.
    return {t: {row[1]: tuple(row) for row in connection.execute(f"PRAGMA table_info({t})")} for t in tables}


def _declared_shape() -> dict[str, dict[str, tuple[Any, ...]]]:
    """What:data:`_SCHEMA` actually produces, read back from a throwaway database.

    Derived rather than written out a second time. A hand-maintained list of the columns we
    expect is a second source of truth for the schema, and the first thing to drift from it
    is the schema.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(_SCHEMA)
        return _shape(probe)
    finally:
        probe.close()


def _add_column_sql(table: str, column: tuple[Any, ...]) -> str:
    """``ALTER TABLE … ADD COLUMN`` for one declared column, or an explanation of why not.

    SQLite cannot add a ``NOT NULL`` column without a default, and cannot add a primary key
    at all. Those need a rebuild, which is a real migration step rather than a convergence —
    so this raises and says so instead of half-applying one.
    """
    _cid, name, decl_type, notnull, default, pk = column
    if pk:
        raise StoreNeedsUpgrade(f"{table}.{name} is a primary key — SQLite cannot add one to an existing table")
    if notnull and default is None:
        raise StoreNeedsUpgrade(f"{table}.{name} is NOT NULL with no default — it cannot be added to existing rows")
    clause = f"{name} {decl_type}"
    if notnull:
        clause += " NOT NULL"
    if default is not None:
        clause += f" DEFAULT {default}"
    return f"ALTER TABLE {table} ADD COLUMN {clause}"


def _missing(connection: sqlite3.Connection) -> dict[str, set[str]]:
    """Declared tables/columns this file does not have, by name.

    Names, not the full ``table_info`` rows: a column added by ``ALTER TABLE`` lands at a
    different ordinal than the same column created by ``_SCHEMA``, so comparing rows would
    report a converged file as still short and migrate it on every open.
    """
    declared, actual = _declared_shape(), _shape(connection)
    absent = {}
    for table, columns in declared.items():
        gap = set(columns) - set(actual.get(table, {}))
        if gap:
            absent[table] = gap
    return absent


def _converge(connection: sqlite3.Connection) -> bool:
    """Bring an existing file up to:data:`_SCHEMA`. Returns whether anything changed.

    Tables AND columns. ``CREATE TABLE IF NOT EXISTS`` adopts a file missing a *column*
    and the stamp is sticky, so a mis-stamped file can never be told from a good one
    afterwards. Convergence is measured against the declared shape, never the stamp.
    """
    absent = _missing(connection)
    if not absent:
        return False
    declared, present = _declared_shape(), set(_shape(connection))
    # Every CREATE is IF NOT EXISTS, so this adds whole missing tables (and the index) and
    # leaves everything else alone. Columns missing from a table that DOES exist need ALTER.
    statements = [_SCHEMA]
    for table, columns in absent.items():
        if table in present:
            statements += [_add_column_sql(table, declared[table][name]) + ";" for name in sorted(columns)]
    with connection:
        for statement in statements:
            connection.executescript(statement)
    return True


def connect(path: Path | str, *, create: bool = False, read_only: bool = False) -> Store:
    """Open the artifact at ``path``. The caller closes it.

    Every caller states which of three it wants:

    - ``connect(p)`` — an existing store, writable. Never creates, so a mistyped path is an
      error where it was typed, not an empty store the data appears to be missing from.
    - ``connect(p, create=True)`` — the first import, the only moment one should come into
      existence.
    - ``connect(p, read_only=True)`` — SQLite's own read-only mode, so a read takes no write
      lock and a restored read-only backup opens at all.

    Refuses a file from a newer baraza: today's queries do not raise against tomorrow's
    schema, they just never ask for the columns they do not know, so the organizer gets a
    complete-looking answer computed from part of their data. Refuses a database that is
    not ours rather than writing our tables into it.
    """
    if create and read_only:
        raise ValueError("create and read_only are contradictory — a read cannot bring a store into existence")

    location = Path(path)
    if not location.exists():
        if not create:
            raise StoreNotFound(
                f"there is no baraza store at {location}. Run `baraza import <folder>` to make one, "
                "or point --store at an existing file."
            )
        try:
            return _create(location)
        except sqlite3.OperationalError as exc:
            # Creating the file failed: the parent directory does not exist, or the location
            # is not writable. Raw, SQLite names neither the path nor the cause.
            raise StoreUnreadable(
                f"{location} could not be created — check its parent directory exists and is writable."
            ) from exc

    try:
        connection = _open(location, read_only=read_only)
    except sqlite3.OperationalError as exc:
        # `unable to open database file` — the path is a directory, its parent is missing, or
        # it is unreadable. Wrapped so the CLI and the web handler both name the path.
        raise StoreUnreadable(
            f"{location} could not be opened as a baraza store — check it is a file (not a "
            "folder) and that its parent directory exists."
        ) from exc
    try:
        found: int = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = set(_shape(connection))
        if not tables:
            # An empty file — ours, half-written, or somebody's `touch`. Treated as "not yet a
            # store" rather than as a foreign database, because that is what it is.
            if not create:
                raise StoreNotFound(f"{location} is empty — it is not a baraza store yet")
        elif not _OUR_TABLES <= tables:
            raise StoreNotBaraza(
                f"{location} is a database, but not a baraza one (it has no {sorted(_OUR_TABLES - tables)} table). "
                "Point --store at your baraza file — writing into this one would corrupt whatever owns it."
            )
        if found > SCHEMA_VERSION:
            raise StoreVersionError(
                f"{location} was written by a newer baraza (schema v{found}; this one reads v{SCHEMA_VERSION}). "
                "Upgrade baraza, or point it at a different file — reading it here would silently drop "
                "whatever the newer version stores."
            )
        _reconcile(connection, found=found, read_only=read_only, location=location)
    except sqlite3.DatabaseError as exc:
        # The file opened but is not a SQLite database — a text file, an image, random bytes.
        # Wrapped so a store replaced mid-serve reaches the web handler as a sentence.
        connection.close()
        raise StoreNotBaraza(
            f"{location} is not a baraza store — it is not a readable SQLite database. "
            "Point --store at your baraza file."
        ) from exc
    except BaseException:
        # Every raise above happens with the connection open. Closing it here is the
        # difference between a refused file and a refused file plus a leaked handle, which
        # on Windows is also a file nobody can then delete.
        connection.close()
        raise
    return Store(connection)


def _create(location: Path) -> Store:
    connection = sqlite3.connect(location)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.executescript(_SCHEMA)
            # PRAGMA takes no bound parameters — verified, the assignment form rejects `?`
            # too. The value is this module's own int constant, never a caller's.
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        connection.close()
        raise
    return Store(connection)


def _open(location: Path, *, read_only: bool) -> sqlite3.Connection:
    """The connection itself, read-only via SQLite's URI mode when asked.

    ``mode=ro`` is what makes a read genuinely a read: no write lock, and no creating the
    file if the path was a typo — a URI open of a missing file fails instead.

    The path is percent-encoded before it goes into the URI, or SQLite decodes it itself: a
    file named ``report%20final.db`` opens the space-named one instead, and a ``#`` splits
    ``?mode=ro`` off as a fragment, losing read-only and creating the wrong file. ``:`` and
    ``/`` stay literal so a Windows drive and the separators survive.
    """
    if read_only:
        encoded = quote(location.as_posix(), safe="/:")
        connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(location)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        connection.close()
        raise
    return connection


def _reconcile(connection: sqlite3.Connection, *, found: int, read_only: bool, location: Path) -> None:
    """Converge an existing file on the declared schema, or refuse to pretend it is current.

    The stamp is a hint; the shape is the fact. A file can carry the current version and
    still be missing a column, so trusting the stamp leaves it broken forever.
    """
    if read_only:
        # A read must not migrate. It also must not answer from a file it knows is short of
        # the schema, because the answer would be wrong rather than missing.
        if found < SCHEMA_VERSION or _missing(connection):
            raise StoreNeedsUpgrade(
                f"{location} was written by an older baraza and this is a read-only open. "
                "Open it once for writing — `baraza import` or `baraza serve` — to upgrade it."
            )
        return
    changed = _converge(connection)
    if changed or found != SCHEMA_VERSION:
        with connection:
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def open_store(path: Path | str, *, create: bool = False, read_only: bool = False) -> Iterator[Store]:
    """:func:`connect` as a context manager, which is how the CLI and the server
    should both take it."""
    store = connect(path, create=create, read_only=read_only)
    try:
        yield store
    finally:
        store.close()


__all__ = [
    "SCHEMA_VERSION",
    "Store",
    "StoreError",
    "StoreNeedsUpgrade",
    "StoreNotBaraza",
    "StoreNotFound",
    "StoreUnreadable",
    "StoreVersionError",
    "StoredAttendance",
    "StoredEvent",
    "StoredMember",
    "connect",
    "open_store",
]
