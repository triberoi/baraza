"""Serving the built front end (``baraza.web.ui``), and the wiring that puts it in
the wheel.

Everything here is driven against temporary directories rather than the real
``web/static``, because that directory's presence is exactly what varies: it is
gitignored, built at release, and therefore **absent in CI and in any fresh clone**.
A suite that asserted against whatever happened to be on disk would pass locally for
whoever last ran ``npm run build`` and mean nothing anywhere else.

The last two tests are the ones with teeth: the assets are excluded from git, so
something has to put them back into the wheel on purpose. Without that line the release
builds green, uploads cleanly, installs cleanly — and the organizer gets a 503 explaining
a fault they cannot fix.
"""

import re
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from baraza.store import connect
from baraza.web.app import TOKEN_HEADER, create_app
from baraza.web.ui import INDEX, STATIC_DIR, is_built, mount_ui

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _serving(static_dir: Path) -> TestClient:
    app = FastAPI()
    mount_ui(app, static_dir)
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def built(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / INDEX).write_text("<!doctype html><title>baraza</title>", encoding="utf-8")
    (static / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    return static


# --- is there a UI at all ------------------------------------------------------
def test_an_absent_directory_is_not_a_built_ui(tmp_path: Path) -> None:
    assert not is_built(tmp_path / "nothing-here")


def test_an_empty_directory_is_not_a_built_ui(tmp_path: Path) -> None:
    """What an interrupted build leaves behind. Without this check it mounts
    cleanly and serves nothing, which is the same symptom as a broken install and a
    different cause."""
    (tmp_path / "static").mkdir()
    assert not is_built(tmp_path / "static")


def test_an_index_makes_it_built(built: Path) -> None:
    assert is_built(built)


# --- the unbuilt case, which is what a clone looks like ---------------------------
def test_an_unbuilt_checkout_explains_itself(tmp_path: Path) -> None:
    """A bare 404 at the root of a tool someone just installed is indistinguishable
    from a broken install. This says which it is and what to run."""
    response = _serving(tmp_path / "nothing-here").get("/")
    assert response.status_code == 503
    assert "npm run build" in response.text
    assert "cd ui" in response.text, "the path must be relative to the package, not to any host tree"


def test_the_unbuilt_page_tells_a_pypi_user_it_is_not_their_fault(tmp_path: Path) -> None:
    """The same page reaches two audiences. A developer needs a command; someone who
    ran `pip install baraza` needs to know there is nothing for them to fix."""
    assert "packaging fault" in _serving(tmp_path / "nothing-here").get("/").text


# --- the built case ---------------------------------------------------------------
def test_the_root_serves_the_page(built: Path) -> None:
    response = _serving(built).get("/")
    assert response.status_code == 200
    assert "baraza" in response.text


def test_a_deep_path_serves_the_page_too(built: Path) -> None:
    """The single-page fallback. Reloading on /people must reach index.html — a 404
    there is a route the organizer can get to and then cannot refresh."""
    assert _serving(built).get("/people/mem_0001").status_code == 200


def test_the_assets_are_served(built: Path) -> None:
    assert _serving(built).get("/assets/index-abc.js").status_code == 200


@pytest.mark.parametrize("path", ["/people/assets/index-abc.js", "/assets/gone.js", "/a/b/assets/x.css"])
def test_a_missing_asset_is_a_404_and_never_the_page(built: Path, path: str) -> None:
    """The bug this exists for, and it cost a live walk to find.

    Vite was configured with a **relative** asset base, so on `/people/mem_0001` the
    browser asked for `/people/assets/index-*.js`. The fallback answered **200 with
    index.html**, because it answered every unknown path that way — the browser got a
    page where it expected a module, the script never parsed, and the screen went
    silently blank with nothing in the console. `/` and `/people` worked; only the
    nested route broke, which is why it survived to a walk.

    The base is absolute now. This is the second half: a request that is plainly for a
    file gets a 404, so the same mistake announces itself instead of hiding.
    """
    response = _serving(built).get(path)
    assert response.status_code == 404
    assert "<!doctype html" not in response.text.lower()


def test_the_built_page_asks_for_its_assets_from_the_root() -> None:
    """The first half, checked against the real build when there is one. A relative
    `src="./assets/…"` is the defect above; only an absolute path survives a nested
    route. Skipped in CI and on a fresh clone, where nothing has been built."""
    index = STATIC_DIR / INDEX
    if not index.is_file():
        pytest.skip("no build in this checkout — the `ui` CI job covers the build itself")
    for reference in re.findall(r'(?:src|href)="([^"]+)"', index.read_text(encoding="utf-8")):
        if reference.startswith(("http://", "https://", "data:", "#")):
            continue
        assert reference.startswith("/"), (
            f"index.html refers to {reference!r} relatively; on a nested route that resolves "
            "under the route and the page silently fails to boot. Vite `base` must be '/'."
        )


def test_a_build_with_no_assets_directory_still_boots(tmp_path: Path) -> None:
    """Vite only emits `assets/` when something was too large to inline. Mounting a
    directory that is not there raises at startup, which would turn a small build
    into a server that will not boot."""
    static = tmp_path / "static"
    static.mkdir()
    (static / INDEX).write_text("<!doctype html>", encoding="utf-8")
    assert _serving(static).get("/").status_code == 200


# --- how it sits with the API -------------------------------------------------------
def test_the_page_does_not_need_the_token_but_the_api_does(tmp_path: Path) -> None:
    """Deliberate, and the reverse would deadlock the tool: this page is what
    *receives* the token from the URL and starts sending it, so gating it would mean
    the browser cannot load the page that is about to authenticate."""
    connect(tmp_path / "b.db", create=True).close()
    app = create_app(tmp_path / "b.db", resolve=None)
    client = TestClient(app, base_url="http://127.0.0.1")

    assert client.get("/").status_code in (200, 503), "the page is reachable with no token either way"
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={TOKEN_HEADER: app.state.token}).status_code == 200


def test_the_api_still_wins_over_the_catch_all(tmp_path: Path) -> None:
    """The UI claims `/{path:path}`, so it must be mounted last. If it were not, it
    would swallow every API route and the whole app would serve HTML."""
    connect(tmp_path / "b.db", create=True).close()
    app = create_app(tmp_path / "b.db", resolve=None)
    client = TestClient(app, base_url="http://127.0.0.1", headers={TOKEN_HEADER: app.state.token})

    body = client.get("/api/status")
    assert body.status_code == 200
    assert body.json()["events"] == 0, "an /api path returned the HTML shell instead of JSON"


def test_the_browser_guards_still_apply_to_the_page(tmp_path: Path) -> None:
    """The token is not required here; the Host and Origin checks still are. They are
    middleware precisely so a route added outside the API cannot opt out of them."""
    connect(tmp_path / "b.db", create=True).close()
    client = TestClient(create_app(tmp_path / "b.db", resolve=None), base_url="http://127.0.0.1")
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    assert client.get("/", headers={"origin": "http://evil.example"}).status_code == 403


# --- the packaging trap ---------------------------------------------------------------
def test_the_wheel_is_told_to_carry_the_built_assets() -> None:
    """`web/static/` is gitignored, and
    hatchling excludes gitignored paths by default — so without this declaration the
    release builds green, uploads cleanly, installs cleanly, and every user gets the
    503 page above for a fault they cannot fix.

    Read from the SHARED build table. On the wheel target alone it does not reach the
    sdist, and `python -m build` builds the wheel from the sdist — see the two guards in
    `test_package.py`, which is where that pair is pinned."""
    manifest = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    artifacts = manifest["tool"]["hatch"]["build"]["artifacts"]
    assert any("web/static" in entry for entry in artifacts), (
        f"the wheel must be told to include the built UI; artifacts are {artifacts}. "
        "It is gitignored, so hatchling drops it unless asked."
    )


def test_the_built_assets_are_not_committed() -> None:
    """The other half of the same rule, checked in this direction too: build output in git
    is output that can disagree with its source. The two lines only make sense together —
    ignored by git, named to the wheel.

    The ignore file is searched for rather than assumed, because where it sits depends on
    how the package is checked out: at the package root on its own, higher up when it is
    inside a larger tree. Only the tail of the entry is asserted, which is the part that is
    the same either way.
    """
    entry = "src/baraza/web/static/"
    for directory in (PACKAGE_ROOT, *PACKAGE_ROOT.parents):
        candidate = directory / ".gitignore"
        if candidate.is_file() and entry in candidate.read_text(encoding="utf-8"):
            break
    else:
        raise AssertionError(
            f"no .gitignore at or above {PACKAGE_ROOT} ignores {entry!r}. Committed build "
            "output can disagree with the source it was built from."
        )
    assert STATIC_DIR.name == "static"


@pytest.mark.parametrize(
    "path", ["/favicon.ico", "/manifest.json", "/robots.txt", "/sw.js", "/people/main.js.map"]
)
def test_any_request_for_a_file_is_a_404_and_never_the_page(built: Path, path: str) -> None:
    """The guard covered ``assets/`` only, so every OTHER file request
    reproduced the exact failure it was written to prevent — a browser's automatic
    ``/favicon.ico``, a service worker, a source map, all answered with HTML and a 200.
    The comment claimed it caught "a request that is plainly for a file"; it caught one
    directory name."""
    assert _serving(built).get(path).status_code == 404


@pytest.mark.parametrize("path", ["/", "/people", "/people/mem_0001", "/retention", "/a/b/c"])
def test_a_route_the_app_invented_still_gets_the_page(built: Path, path: str) -> None:
    """The other side of it, and the reason the file check is a heuristic rather
    than a list: widening it must not start 404ing the routes the app routes to itself.
    None of them contain a dot, which is what makes the heuristic safe here — and this
    test is what would notice if one ever did."""
    assert _serving(built).get(path).status_code == 200


def test_an_unknown_api_path_is_a_404_and_never_the_page(built: Path) -> None:
    """At the layer that causes it. The catch-all is mounted at the root and
    runs after the API's own routes, so a mistyped in-app URL reached it and came back as
    the front page with a **200**. The caller then parsed HTML as JSON and the organizer
    saw a raw parser error rather than "no such thing"."""
    client = _serving(built)
    for path in ("/api", "/api/nope", "/api/people/extra"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "<!doctype" not in response.text.lower(), path


def test_the_api_prefix_is_declared_once() -> None:
    """The catch-all has to refuse exactly what the API claims, and two copies of that
    fact would drift the first time one moved — which is the same shape in the
    first place. ``app`` re-exports the constant this module declares rather than
    repeating it, and this pins that they are the same object."""
    from baraza.web.app import GUARDED_PREFIX
    from baraza.web.ui import API_PREFIX

    assert GUARDED_PREFIX == API_PREFIX == "/api"


# --- both branches, not just the one this machine happens to have --------------------
def _unbuilt(tmp_path: Path) -> TestClient:
    """A server with no built UI — which is every source checkout, and CI."""
    app = FastAPI()
    mount_ui(app, tmp_path / "nothing-here")
    return TestClient(app)


@pytest.mark.parametrize("path", ["/api", "/api/nope", "/api/people/extra"])
def test_an_unknown_api_path_is_a_404_in_a_source_checkout_too(tmp_path: Path, path: str) -> None:
    """It lived in BOTH branches of ``mount_ui``, and this is the one a developer
    meets every time — a clone has no built UI by design.

    Unfixed, a mistyped ``/api/…`` came back as the "interface has not been built" page
    with a 503, so the caller parsed markup as JSON and got a syntax error, with a
    confusing page as the only evidence. Fixing the built branch alone would have looked
    complete on a machine that had run ``npm run build``.
    """
    response = _unbuilt(tmp_path).get(path)
    assert response.status_code == 404
    assert "<!doctype" not in response.text.lower()


def test_the_unbuilt_page_still_answers_everything_else(tmp_path: Path) -> None:
    """The refusal must not swallow the page it exists to serve: an organizer who
    installed from PyPI and hit a packaging fault still needs the explanation."""
    for path in ("/", "/people", "/retention"):
        assert _unbuilt(tmp_path).get(path).status_code == 503, path


def test_both_branches_refuse_the_same_things(tmp_path: Path, built: Path) -> None:
    """The property, rather than two lists that can drift. Whether or not a UI has been
    built changes what a page request ANSWERS — the app or an explanation — and must not
    change what counts as a request for the page at all."""
    unbuilt, serving = _unbuilt(tmp_path), _serving(built)
    for path in ("/api/nope", "/favicon.ico", "/manifest.json", "/people/main.js.map"):
        assert unbuilt.get(path).status_code == 404 == serving.get(path).status_code, path
    for path in ("/", "/people", "/people/mem_0001"):
        assert unbuilt.get(path).status_code == 503, path
        assert serving.get(path).status_code == 200, path
