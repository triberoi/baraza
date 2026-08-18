"""Serving the built front end out of the wheel.

The assets are built at release, not committed, so a source checkout has no UI and
:func:`mount_ui` explains that rather than 404ing.

SECURITY: the page is deliberately unauthenticated. It holds no data, and it is what
receives the token from the URL and starts sending it. ``/api`` is what is guarded.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

#: Where the Vite build lands, inside the installed package.
STATIC_DIR = Path(__file__).parent / "static"

#: What a built UI must contain. An interrupted build leaves a directory that mounts
#: cleanly and serves nothing.
INDEX = "index.html"

#: Everything the JSON API answers on. Declared here and re-exported by
#: :mod:`baraza.web.app` as ``GUARDED_PREFIX`` — the other direction would be a cycle.
#: The catch-all below must refuse exactly what the API claims, so there is one constant.
API_PREFIX = "/api"

_NOT_BUILT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>baraza — UI not built</title>
<style>
  body {{ font: 16px/1.55 system-ui, sans-serif; color: #30435B;
          max-width: 34rem; margin: 4rem auto; padding: 0 1rem; }}
  code {{ background: #EEF2F6; padding: 0.25rem; border-radius: 4px; }}
  p {{ margin:.75rem 0; }}
</style></head><body>
<h1>The interface has not been built</h1>
<p>The API is running and answering — this is a source checkout, where the front end
is compiled at release rather than committed.</p>
<p>To build it: <code>cd {ui}</code> then <code>npm install &amp;&amp; npm run build</code>,
and reload.</p>
<p>If you installed baraza from PyPI and are seeing this, that is a packaging fault in
the release, not something you can fix locally — please report it.</p>
</body></html>"""


def is_built(static_dir: Path = STATIC_DIR) -> bool:
    """Whether there is a UI to serve. An empty directory is not one."""
    return (static_dir / INDEX).is_file()


def _refuse_if_not_the_page(path: str) -> None:
    """404 the API prefix and anything with a dot in its last segment.

    Answering these with markup makes a caller parse HTML as JSON and report a syntax
    error instead of the real cause. The app's own routes contain no dots.
    """
    api = API_PREFIX.strip("/")
    if path == api or path.startswith(f"{api}/"):
        raise HTTPException(status_code=404, detail=f"no such endpoint: /{path}")
    if "." in path.rsplit("/", 1)[-1]:
        raise HTTPException(status_code=404, detail=f"no such file: /{path}")


def mount_ui(app: FastAPI, static_dir: Path = STATIC_DIR) -> None:
    """Serve the built UI at ``/``, or an explanation if there is none.

    Mounted last, at the root, so it catches everything ``/api`` did not and a reload on
    an app-invented route reaches ``index.html``.
    """
    if not is_built(static_dir):

        @app.get("/{path:path}", include_in_schema=False)
        def not_built(path: str) -> HTMLResponse:
            _refuse_if_not_the_page(path)
            return HTMLResponse(_NOT_BUILT.format(ui="ui"), status_code=503)

        return

    # Vite only emits `assets/` when something was too big to inline, and mounting a
    # directory that is not there raises at startup.
    assets = static_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def index(path: str) -> FileResponse:
        """Any unknown path gets the page, except one plainly not for it: answering
        ``…/assets/main.js`` with HTML and a 200 turns a wrong URL into a blank screen."""
        _refuse_if_not_the_page(path)
        return FileResponse(static_dir / INDEX)


__all__ = ["API_PREFIX", "INDEX", "STATIC_DIR", "is_built", "mount_ui"]
