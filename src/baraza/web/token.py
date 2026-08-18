"""The session token.

SECURITY: loopback keeps other machines out, but any process on this one can reach
127.0.0.1. So ``/api`` requires the token, and the token lives in a file only the
organizer can read — the permissions are the control (:mod:`baraza.credentials`).
Delete the file to rotate it.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from baraza.credentials import TOKEN_SUFFIX, read_secret, secret_path, write_secret

#: Bytes of randomness. Never typed by a human, so length costs nothing.
TOKEN_BYTES = 32


def token_path(store_path: Path | str) -> Path:
    """Where the token for ``store_path`` lives."""
    return secret_path(store_path, TOKEN_SUFFIX)


def ensure_token(store_path: Path | str) -> str:
    """The token for this store, creating one if there is none."""
    existing = read_secret(store_path, TOKEN_SUFFIX)
    if existing is not None:
        return existing
    token = secrets.token_urlsafe(TOKEN_BYTES)
    write_secret(store_path, TOKEN_SUFFIX, token)
    return token


__all__ = ["TOKEN_BYTES", "TOKEN_SUFFIX", "ensure_token", "token_path"]
