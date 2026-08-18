"""The browser boundary: ``baraza.web.app``'s Host and Origin checks.

Loopback stops packets from other machines, not the organizer's own browser — and this
server hands out the whole roster behind no login, by design. Two attacks get there
through the browser and each check answers exactly one:

- DNS rebinding: a hostile page resolving to 127.0.0.1 is same-origin to the browser. The
  request arrives under the ATTACKER's name, which the ``Host`` check refuses.
- CSRF: that page can instead ``fetch`` 127.0.0.1 directly, so the ``Host`` header is
  genuinely ours. The browser withholds the response, never the side effect, and a
  ``POST`` of ``text/plain`` never preflights. The ``Origin`` check refuses that.

Neither substitutes for the other, and a test below says so.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baraza.store import connect
from baraza.web.app import ALLOWED_HOSTS, TOKEN_HEADER, create_app, hostname_of

NOW = datetime(2026, 6, 1, tzinfo=UTC)

#: What a hostile page sends: our Host, their Origin.
FOREIGN = {"origin": "http://evil.example"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    connect(tmp_path / "b.db", create=True).close()
    app = create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None)
    return TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: app.state.token})


# --- parsing the header ------------------------------------------------------------
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("127.0.0.1", "127.0.0.1"),
        ("127.0.0.1:53219", "127.0.0.1"),
        ("localhost", "localhost"),
        ("localhost:8000", "localhost"),
        # Bracketed IPv6 is why this is a function and not an rsplit — the naive
        # version turns `[::1]` into `[:` and rejects the address it exists to admit.
        ("[::1]", "[::1]"),
        ("[::1]:8000", "[::1]"),
        ("evil.example", "evil.example"),
        ("evil.example:80", "evil.example"),
        ("", ""),
    ],
)
def test_the_hostname_is_taken_off_the_header_correctly(header: str, expected: str) -> None:
    assert hostname_of(header) == expected


def test_every_loopback_form_the_parser_produces_is_admitted() -> None:
    """The two halves have to agree. A parser that emits `[::1]` against an allow-list
    holding `::1` would reject a legitimate request, and the fix would be to widen the
    list rather than notice the mismatch."""
    for header in ("127.0.0.1:1", "localhost:1", "[::1]:1"):
        assert hostname_of(header) in ALLOWED_HOSTS


# --- the guard itself ----------------------------------------------------------------
def test_a_request_addressed_to_another_name_is_refused(client: TestClient) -> None:
    """The attack, as close as a test can get to it: the socket is reached, and the
    only thing wrong is the name in the header."""
    assert client.get("/api/people", headers={"host": "evil.example"}).status_code == 421


def test_a_request_addressed_to_this_machine_is_served(client: TestClient) -> None:
    for host in ("127.0.0.1", "127.0.0.1:53219", "localhost:8000", "[::1]:8000"):
        assert client.get("/api/people", headers={"host": host}).status_code == 200


def test_a_missing_host_is_refused(client: TestClient) -> None:
    """HTTP/1.1 requires one, so its absence means a hand-rolled client rather than the
    browser this server exists for. A guard that waves through the case it did not
    think about is not a guard."""
    assert client.get("/api/people", headers={"host": ""}).status_code == 421


def test_the_guard_covers_writes_as_well_as_reads(client: TestClient) -> None:
    """Middleware rather than a per-route dependency, so a route added later cannot
    forget to ask for it. The import endpoint is the one that would hurt: a hostile
    page that could reach it could point the tool at any folder on the machine."""
    refused = {
        "PUT /api/thresholds": client.put("/api/thresholds", json={}, headers={"host": "evil.example"}),
        "POST /api/import/folder": client.post(
            "/api/import/folder", json={"path": "."}, headers={"host": "evil.example"}
        ),
        "PUT /api/events/x": client.put("/api/events/x", json={}, headers={"host": "evil.example"}),
    }
    assert {name: response.status_code for name, response in refused.items()} == dict.fromkeys(refused, 421)


# --- the other browser attack: CSRF ------------------------------------------------------
#
# The Host check above answers a request arriving under the ATTACKER's name. This half
# answers one arriving under OURS. A page on evil.example can fetch 127.0.0.1 directly:
# the Host header is genuinely `127.0.0.1`, so the rebinding guard waves it through, the
# browser sends it, and the only thing the browser withholds is the RESPONSE. The side
# effect happens either way.


def test_the_import_endpoint_is_not_csrf_able(client: TestClient, tmp_path: Path) -> None:
    """The vector the security scan named, as close to literal as a test gets: a simple
    request — POST, ``text/plain``, so no preflight can refuse it — carrying JSON that
    FastAPI decodes regardless of the declared content type. Without the Origin check
    this imported whatever folder the attacker chose."""
    response = client.post(
        "/api/import/folder",
        content=f'{{"path": "{tmp_path.as_posix()}"}}',
        headers={"content-type": "text/plain", "origin": "http://evil.example"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("method", ["post", "put"])
def test_a_write_from_another_site_is_refused(client: TestClient, method: str) -> None:
    call = getattr(client, method)
    response = call("/api/thresholds" if method == "put" else "/api/import/folder", json={}, headers=FOREIGN)
    assert response.status_code == 403


def test_a_read_from_another_site_is_refused_too(client: TestClient) -> None:
    """Belt and braces. The browser already withholds a cross-origin response body, but
    a guard that depends on the browser doing its job is not a guard — and this costs a
    legitimate client nothing, because a legitimate client never has a foreign Origin."""
    assert client.get("/api/people", headers=FOREIGN).status_code == 403


def test_a_write_with_no_origin_at_all_is_refused(client: TestClient) -> None:
    """Browsers send Origin on every non-GET request, same-origin included, so the
    tool's own UI is unaffected. What this rejects is a hand-rolled client — and the
    answer for scripting is the CLI, which calls the library rather than the socket."""
    assert client.put("/api/thresholds", json={}).status_code == 403


def test_a_plain_read_with_no_origin_still_works(client: TestClient) -> None:
    """A same-origin GET, which is most of what the UI does. The browser omits Origin
    there, and refusing it would break the tool to defend against nothing."""
    assert client.get("/api/people").status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:53219",
        "http://localhost:8000",
        "http://[::1]:8000",
        "https://127.0.0.1:8443",
    ],
)
def test_the_tools_own_ui_is_admitted(client: TestClient, origin: str) -> None:
    """Every origin the UI can legitimately be served from. A guard that rejected one of
    these would be found by a user and "fixed" by widening it until it stopped working."""
    assert client.get("/api/people", headers={"origin": origin}).status_code == 200
    assert client.put("/api/thresholds", json={}, headers={"origin": origin}).status_code != 403


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "https://evil.example",
        # A name that merely CONTAINS a loopback name. Substring matching would admit
        # every one of these, and registering `127.0.0.1.evil.example` costs nothing.
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "http://evil.example/127.0.0.1",
        # A different scheme entirely — a browser extension or a file:// page.
        "null",
        "file://",
    ],
)
def test_a_lookalike_origin_is_not_mistaken_for_ours(client: TestClient, origin: str) -> None:
    """Parsed as a URL and compared on the host, not searched for a substring. This is
    the shape almost every hand-rolled origin check gets wrong."""
    assert client.get("/api/people", headers={"origin": origin}).status_code == 403


def test_the_two_guards_answer_different_attacks(client: TestClient) -> None:
    """Stated as a test because it is the thing that was missed the first time: a
    request can pass either check and fail the other, so neither substitutes for the
    other and removing one is not a simplification."""
    rebinding = client.get("/api/people", headers={"host": "evil.example"})
    csrf = client.get("/api/people", headers={"host": "127.0.0.1", "origin": "http://evil.example"})
    assert (rebinding.status_code, csrf.status_code) == (421, 403)


def test_every_route_on_the_app_is_behind_the_guard(client: TestClient) -> None:
    """Enumerated from the app rather than listed by hand, so a route added in a later
    phase is covered by this test on the day it is written — a hand-kept list would
    silently stop covering the newest thing, which is always the least reviewed."""
    def walk(routes: object) -> list[str]:
        # FastAPI wraps an included router rather than flattening it, so the API's own
        # routes are one level down. Recursive, because the assertion below is the only
        # thing standing between "the app changed shape" and this test passing on an
        # empty set — which is the failure mode a hand-kept list would have had anyway.
        found = []
        for route in routes:  # type: ignore[attr-defined]
            path = getattr(route, "path", None)
            if isinstance(path, str):
                found.append(path)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                found.extend(walk(inner.routes))
        return found

    paths = {p for p in walk(client.app.routes) if p.startswith("/api")}
    assert len(paths) >= 8, f"expected every /api route, found {sorted(paths)} — this test is reading the wrong thing"
    for path in paths:
        response = client.get(path.replace("{member_id}", "x").replace("{event_id}", "x"), headers={"host": "evil.x"})
        assert response.status_code == 421, f"{path} answered a request addressed to another name"
