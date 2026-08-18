"""Serving the real app to a real browser, on a store built for one test.

The app is started exactly as ``baraza serve`` starts it — :func:`baraza.web.prepare`,
then uvicorn on the bound socket — so the walk exercises the token in the URL, the
static mount and the API together. Anything less would be testing a rearrangement of
the app rather than the app.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

from baraza.store import StoredAttendance, StoredEvent, StoredMember, connect
from baraza.web import prepare
from baraza.web.ui import is_built

D = datetime(2026, 6, 1, tzinfo=UTC)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse to pretend. Without a built front end every page is the 503 "not built"
    notice, and a walk against it would pass its way through a set of assertions about
    nothing. A whole-run failure is the honest outcome — the CI job builds the UI first,
    and the README says how to do it locally."""
    if not is_built():
        pytest.exit(
            "The front end is not built, so there is nothing to walk. "
            "Run: cd ui && npm ci && npm run build",
            returncode=1,
        )


@pytest.fixture
def calendar(tmp_path: Path) -> Path:
    """A store with enough shape for every screen to have something to say: a champion,
    a no-show, someone who never came, and a month with no event in it."""
    path = tmp_path / "walk.db"
    store = connect(path, create=True)
    def event(event_id: str, title: str, days_ago: int) -> StoredEvent:
        starts = D - timedelta(days=days_ago)
        return StoredEvent(event_id, title, starts, starts + timedelta(hours=2))

    events = [
        event("evt-jan", "January meetup", 150),
        event("evt-feb", "February meetup", 120),
        event("evt-mar", "March meetup", 90),
        # No April or May: the "no event that month" cell has to have something to be.
        event("evt-jun", "June meetup", 1),
        # Application-gated, so the funnel columns and their caveat have something to
        # render. Without one on the calendar the whole surface is unreachable by a walk.
        event("evt-dinner", "March supper club", 89),
    ]
    members = [
        StoredMember("mem_champ", "champ@example.com", "Cam Champion"),
        StoredMember("mem_once", "once@example.com", "Robin Once"),
        StoredMember("mem_ghost", "ghost@example.com", "Sam Ghost"),
        StoredMember("mem_waiting", "waiting@example.com", "Ada Waiting"),
        StoredMember("mem_turned", "turned@example.com", "Lee Turned"),
    ]
    attendances = [
        *(StoredAttendance(e.event_id, "mem_champ", "attended", e.starts_at, e.starts_at) for e in events),
        StoredAttendance("evt-jan", "mem_once", "attended", events[0].starts_at, events[0].starts_at),
        StoredAttendance("evt-jun", "mem_ghost", "no_show", events[3].starts_at),
        # The gated event: one admitted and present, one still in the queue, one declined.
        StoredAttendance(
            "evt-dinner", "mem_champ", "attended", events[4].starts_at, events[4].starts_at,
            luma_approval_status="approved",
        ),
        StoredAttendance(
            "evt-dinner", "mem_waiting", "waitlisted", events[4].starts_at,
            luma_approval_status="pending_approval",
        ),
        StoredAttendance(
            "evt-dinner", "mem_turned", "cancelled", events[4].starts_at,
            luma_approval_status="declined",
        ),
    ]
    store.write_import(events, members, attendances)
    store.close()
    return path


class _Served:
    """A running app and the one URL that can reach it."""

    def __init__(self, url: str, server: uvicorn.Server, thread: threading.Thread) -> None:
        self.url = url
        self._server = server
        self._thread = thread

    @property
    def origin(self) -> str:
        return self.url.split("/?")[0].split("?")[0].rstrip("/")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def _serve(store_path: Path) -> _Served:
    url, sock, app = prepare(store_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    _wait_for(sock)
    return _Served(url, server, thread)


def _wait_for(sock: socket.socket, timeout: float = 10.0) -> None:
    """Uvicorn takes a moment to accept on a socket it was handed. Polling beats a
    fixed sleep: a sleep long enough for a slow CI runner is wasted on every fast one,
    and one short enough to feel quick is a flake."""
    host, port = sock.getsockname()[:2]
    deadline = datetime.now(UTC) + timedelta(seconds=timeout)
    while datetime.now(UTC) < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            continue
    raise RuntimeError(f"the app did not start listening on {host}:{port} within {timeout}s")


@pytest.fixture
def served(calendar: Path) -> Iterator[_Served]:
    """The app, with data. Stopped whatever the test does."""
    app = _serve(calendar)
    try:
        yield app
    finally:
        app.stop()


@pytest.fixture
def empty_store(tmp_path: Path) -> Path:
    path = tmp_path / "empty.db"
    connect(path, create=True).close()
    return path


@pytest.fixture
def served_empty(empty_store: Path) -> Iterator[_Served]:
    """The app with nothing in it — the first thing a new organizer ever sees."""
    app = _serve(empty_store)
    try:
        yield app
    finally:
        app.stop()
