"""The local web app: a JSON API over the artifact, served on loopback.

``app`` holds no arithmetic; every number comes from :mod:`baraza.analytics`.
"""

from __future__ import annotations

from baraza.web.app import (
    ALLOWED_HOSTS,
    UNSAFE_METHODS,
    EventBody,
    ImportFolderBody,
    ThresholdsBody,
    create_app,
    hostname_of,
)
from baraza.web.server import LOOPBACK, bind, prepare, url_for
from baraza.web.token import ensure_token, token_path

__all__ = [
    "ALLOWED_HOSTS",
    "LOOPBACK",
    "UNSAFE_METHODS",
    "EventBody",
    "ImportFolderBody",
    "ThresholdsBody",
    "bind",
    "create_app",
    "ensure_token",
    "prepare",
    "hostname_of",
    "token_path",
    "url_for",
]
