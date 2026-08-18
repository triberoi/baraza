"""``baraza serve`` — the local server.

SECURITY: binds loopback, with deliberately no parameter to change it. The tool has no
login, which is safe only while the socket is unreachable from other machines. Do not
add a host argument; put a reverse proxy in front instead.

The port is ephemeral and the socket is bound here, not in uvicorn, so the URL is known
before the server starts.
"""

from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI

from baraza.web.app import create_app

#: SECURITY: a constant, not a default. See the module docstring.
LOOPBACK = "127.0.0.1"


def bind() -> socket.socket:
    """A listening socket on loopback, on a port the kernel chose."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LOOPBACK, 0))
    sock.listen()
    return sock


def url_for(sock: socket.socket, *, token: str | None = None) -> str:
    """The address to open a browser at, read back from the socket.

    A browser's first contact is a navigation and cannot send a header, so the token
    travels in the URL once and the page keeps it from there.
    """
    host, port = sock.getsockname()[:2]
    base = f"http://{host}:{port}"
    return f"{base}/?{urlencode({'token': token})}" if token else base


def prepare(store_path: Path | str) -> tuple[str, socket.socket, FastAPI]:
    """``(url, socket, app)`` — everything a launcher needs, before it serves anything.

    An unopenable store fails here rather than in the browser; the check is in
    :func:`baraza.web.app.create_app`. A path that does not exist yet is a first run.
    """
    app = create_app(store_path)
    sock = bind()
    return url_for(sock, token=app.state.token), sock, app


__all__ = ["LOOPBACK", "bind", "prepare", "url_for"]
