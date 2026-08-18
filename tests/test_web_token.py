"""The session token (``baraza.web.token`` and the check in ``baraza.web.app``).

The ``Host`` and ``Origin`` checks answer attacks that arrive *through the organizer's
browser*. This answers the one that does not: **every process on the machine can
connect to 127.0.0.1**, whoever it runs as. A second user on the same laptop can ask
for ``/api/people`` and get names, email addresses and attendance history.

The mechanism is one sentence — the port is open to everyone, the token file is not, so
the file is the boundary — and these tests are about the ways that sentence stops being
true: a token that can be skipped, a token compared loosely, a token file left readable.
"""

import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baraza.credentials import TOKEN_SUFFIX, CredentialError, read_secret, secret_path, write_secret
from baraza.store import connect
from baraza.web import prepare
from baraza.web.app import TOKEN_HEADER, TOKEN_PARAM, create_app
from baraza.web.token import ensure_token, token_path

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    connect(path, create=True).close()
    return path


def _app(store: Path):  # noqa: ANN202
    return create_app(store, clock=lambda: NOW, resolve=None)


def _client(app, **headers: str) -> TestClient:  # noqa: ANN001
    return TestClient(app, base_url="http://127.0.0.1", headers=headers)


# --- the file ---------------------------------------------------------------------
def test_the_token_lives_beside_the_store(store: Path) -> None:
    """So the two travel together. An organizer who moves their artifact to another
    machine should not silently leave its token behind and meet a 401 they cannot
    explain."""
    ensure_token(store)
    assert token_path(store) == store.with_name(store.name + ".token")
    assert token_path(store).exists()


def test_the_same_store_keeps_the_same_token(store: Path) -> None:
    """Restarting the server must not invalidate the tab the organizer already has
    open. A token regenerated per process would do exactly that."""
    assert ensure_token(store) == ensure_token(store)


def test_deleting_the_file_rotates_the_token(store: Path) -> None:
    """The whole revocation story, and it is deliberately the only one: there is no
    account to reset and nothing to remember."""
    first = ensure_token(store)
    token_path(store).unlink()
    assert ensure_token(store) != first


def test_an_empty_token_file_is_replaced_rather_than_honored(store: Path) -> None:
    """A truncated file — an interrupted write, a bad copy — must not become a token
    that the empty string matches."""
    ensure_token(store)
    token_path(store).write_text("", encoding="utf-8")
    assert ensure_token(store) != ""


def test_the_token_is_long_enough_to_be_worth_having(store: Path) -> None:
    """A local attacker retries as fast as the machine allows and there is nothing to
    rate-limit them with, so the only defense is length."""
    assert len(ensure_token(store)) >= 32


def test_two_stores_do_not_share_a_token(tmp_path: Path) -> None:
    for name in ("a.db", "b.db"):
        connect(tmp_path / name, create=True).close()
    assert ensure_token(tmp_path / "a.db") != ensure_token(tmp_path / "b.db")


@pytest.mark.skipif(sys.platform == "win32", reason="os.chmod only toggles the read-only bit on Windows")
def test_the_file_is_readable_only_by_its_owner(store: Path) -> None:
    """The file's permissions are the entire control — if another local user can read
    it, the token buys nothing."""
    ensure_token(store)
    assert stat.S_IMODE(token_path(store).stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="os.chmod only toggles the read-only bit on Windows")
def test_a_file_left_world_readable_is_narrowed_on_read(store: Path) -> None:
    """A token written by an earlier version, or widened by a copy, is fixed rather
    than trusted. Reading it and shrugging would leave the one control that matters
    quietly off."""
    ensure_token(store)
    os.chmod(token_path(store), 0o644)
    ensure_token(store)
    assert stat.S_IMODE(token_path(store).stat().st_mode) == 0o600


# --- the check ----------------------------------------------------------------------
def test_the_api_is_closed_without_the_token(store: Path) -> None:
    """The finding this exists for: another process on the machine reaches the socket
    and gets nothing."""
    assert _client(_app(store)).get("/api/people").status_code == 401


def test_the_token_opens_it(store: Path) -> None:
    app = _app(store)
    assert _client(app, **{TOKEN_HEADER: app.state.token}).get("/api/people").status_code == 200


def test_the_token_is_accepted_in_the_url_too(store: Path) -> None:
    """The browser's first contact is a navigation and cannot carry a header, so the
    token has to arrive in the URL at least once for the UI to pick it up."""
    app = _app(store)
    assert _client(app).get(f"/api/people?{TOKEN_PARAM}={app.state.token}").status_code == 200


@pytest.mark.parametrize("presented", ["", "wrong", "  ", "None", "null"])
def test_a_wrong_token_is_refused(store: Path, presented: str) -> None:
    assert _client(_app(store), **{TOKEN_HEADER: presented}).get("/api/people").status_code == 401


def test_a_prefix_of_the_token_is_not_enough(store: Path) -> None:
    """Compared whole, with ``secrets.compare_digest``. A check that accepted a prefix
    would turn an unlimited local retry budget into a short search."""
    app = _app(store)
    assert _client(app, **{TOKEN_HEADER: app.state.token[:-1]}).get("/api/people").status_code == 401


def test_a_token_file_that_is_not_utf8_is_a_sentence_not_a_traceback(store: Path) -> None:
    """A token an editor saved as UTF-16/ANSI raises a raw ``UnicodeDecodeError`` —
    a traceback on ``serve`` startup, a blank 500 per request. ``read_secret`` now raises a
    ``CredentialError`` naming the file and the fix (delete it to regenerate)."""
    token_path(store).write_bytes(b"\xff\xfe\x00\x00garbage-not-utf8")
    with pytest.raises(CredentialError, match=token_path(store).name):
        read_secret(store, TOKEN_SUFFIX)


def test_startup_on_a_corrupt_token_file_is_a_sentence_not_a_traceback(store: Path) -> None:
    """The startup half: ``create_app`` reads (or mints) the token before serving. A
    file that will not decode must raise ``CredentialError`` — which the CLI turns into a
    one-line message — not a raw ``UnicodeDecodeError`` traceback from ``baraza serve``."""
    token_path(store).write_bytes(b"\xff\xfe\x00\x00garbage-not-utf8")
    with pytest.raises(CredentialError, match=token_path(store).name):
        _app(store)


def test_a_corrupt_token_file_is_a_503_not_a_blank_500(store: Path) -> None:
    """The per-request half: the auth middleware reads the token file on every
    request, so a file that stops decoding after the server started must come back as a 503
    sentence that names the file, never a bare 500 on every screen."""
    app = _app(store)  # built while the token is valid, so startup succeeds
    valid = app.state.token
    token_path(store).write_bytes(b"\xff\xfe\x00\x00garbage-not-utf8")
    response = _client(app, **{TOKEN_HEADER: valid}).get("/api/people")
    assert response.status_code == 503
    assert token_path(store).name in response.json()["detail"]


def test_another_stores_token_does_not_open_this_one(tmp_path: Path) -> None:
    for name in ("mine.db", "theirs.db"):
        connect(tmp_path / name, create=True).close()
    theirs = create_app(tmp_path / "theirs.db", clock=lambda: NOW, resolve=None)
    mine = create_app(tmp_path / "mine.db", clock=lambda: NOW, resolve=None)
    assert _client(mine, **{TOKEN_HEADER: theirs.state.token}).get("/api/people").status_code == 401


def test_every_api_route_needs_the_token(store: Path) -> None:
    """Enumerated from the app rather than listed, so a route written in a later phase
    is covered the day it exists — the same reason the Host guard's sweep is built that
    way."""

    def walk(routes: object) -> list[str]:
        found = []
        for route in routes:  # type: ignore[attr-defined]
            path = getattr(route, "path", None)
            if isinstance(path, str):
                found.append(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(walk(inner.routes))
        return found

    client = _client(_app(store))
    paths = {p for p in walk(client.app.routes) if p.startswith("/api")}
    assert len(paths) >= 8, f"expected every /api route, found {sorted(paths)}"
    for path in paths:
        response = client.get(path.replace("{member_id}", "x").replace("{event_id}", "x"))
        assert response.status_code == 401, f"{path} answered without the token"


def test_there_is_no_way_to_build_an_app_without_a_token(store: Path) -> None:
    """An "auth off" switch is a switch somebody ships enabled. The absence is the
    guarantee, so the absence is what is asserted."""
    import inspect

    parameters = inspect.signature(create_app).parameters
    assert not {"token", "auth", "no_auth", "insecure"} & set(parameters)
    assert _app(store).state.token


# --- the handoff ---------------------------------------------------------------------
def test_prepare_hands_back_a_url_that_carries_the_token(store: Path) -> None:
    """How the organizer gets in at all. The URL they are told to open has to work on
    the first click, before any UI exists to hold a token."""
    url, sock, app = prepare(store)
    try:
        assert url.startswith("http://127.0.0.1:")
        assert f"{TOKEN_PARAM}={app.state.token}" in url
        assert str(sock.getsockname()[1]) in url
    finally:
        sock.close()


def test_prepare_fails_before_a_browser_is_opened(tmp_path: Path) -> None:
    """Building the app first means a store that cannot be opened raises here, rather
    than after a browser has been pointed at a server that was never going to work."""
    with pytest.raises(Exception):  # noqa: B017 — the point is that it fails, not how
        prepare(tmp_path / "no-such-directory" / "b.db")


def test_deleting_the_token_file_revokes_a_session_that_is_already_running(tmp_path: Path) -> None:
    """Two comments said the token is "rotated by deleting it" and the
    README said nothing at all — but the value was read once at startup and compared
    against that copy forever, so deleting the file revoked nothing until the next
    `baraza serve`.

    It matters for the one case the token exists for: an organizer who has pasted or
    screenshotted their serve URL and wants the link dead now. Following the only
    documented remedy left the session wide open, with no message saying so.
    """
    store_path = tmp_path / "b.db"
    connect(store_path, create=True).close()
    app = create_app(store_path)
    client = TestClient(app, raise_server_exceptions=False)
    original = app.state.token

    assert client.get("/api/status", headers={TOKEN_HEADER: original, "host": "127.0.0.1"}).status_code == 200

    token_path(store_path).unlink()
    assert client.get("/api/status", headers={TOKEN_HEADER: original, "host": "127.0.0.1"}).status_code == 401


def test_replacing_the_token_file_makes_the_new_value_the_live_one(tmp_path: Path) -> None:
    """The other direction, and the reason this is a re-read rather than a cache
    invalidation: whatever the file says now is what the socket accepts now."""
    store_path = tmp_path / "b.db"
    connect(store_path, create=True).close()
    app = create_app(store_path)
    client = TestClient(app, raise_server_exceptions=False)
    original = app.state.token

    write_secret(store_path, TOKEN_SUFFIX, "a-freshly-rotated-token")

    assert client.get("/api/status", headers={TOKEN_HEADER: original, "host": "127.0.0.1"}).status_code == 401
    fresh = client.get("/api/status", headers={TOKEN_HEADER: "a-freshly-rotated-token", "host": "127.0.0.1"})
    assert fresh.status_code == 200


# --- credential file mechanics -------------------------------------------------
def test_a_trailing_separator_on_the_store_does_not_move_the_credentials(tmp_path: Path) -> None:
    """The suffix attaches to the FILENAME, not to the path as text. Built by string
    concatenation, `store.db/` + `.token` resolved INSIDE a path component that is not a
    directory, so the read failed at the OS with nothing on screen naming the cause.
    """
    store = tmp_path / "b.db"
    assert secret_path(f"{store}{os.sep}", TOKEN_SUFFIX) == secret_path(store, TOKEN_SUFFIX)
    assert secret_path(store, TOKEN_SUFFIX).parent == tmp_path


def test_a_secret_never_lands_in_a_file_that_is_already_permissive(tmp_path: Path) -> None:
    """SECURITY: writing in place narrowed the mode only after the value had arrived, so
    a target left over-permissive by a backup or sync tool exposed the secret for the
    window in between. The value now goes to a fresh 0600 temporary file and is renamed
    over the target, which discards the loose mode with the old inode.

    Two assertions, because the obvious one is POSIX-only. The mode check cannot run on
    Windows (`restrict` is a no-op there), so the property is ALSO stated as file
    identity: a rename-over replaces the inode, and a write-in-place cannot. That half
    holds on every platform, so this test still proves the fix where it is developed and
    not only where it is shipped.
    """
    store = tmp_path / "b.db"
    path = secret_path(store, TOKEN_SUFFIX)
    path.write_text("old", encoding="utf-8")
    os.chmod(path, 0o666)
    before = path.stat().st_ino

    write_secret(store, TOKEN_SUFFIX, "the-new-secret")

    assert read_secret(store, TOKEN_SUFFIX) == "the-new-secret"
    assert path.stat().st_ino != before, "the value was written into the pre-existing file"
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, "the loose mode survived the write"
    assert not list(tmp_path.glob("*.tmp")), "the temporary file was left behind"
