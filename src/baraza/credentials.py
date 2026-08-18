"""The session token and the Luma API key, in files beside the store.

SECURITY: these are 0600 on POSIX, and that is the whole control. On Windows
``os.chmod`` only toggles the read-only bit, leaving the directory's inherited ACL. They
live beside the store, never in it — the store gets copied and sent.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

#: The session token for the local server. Rotated by deleting it.
TOKEN_SUFFIX = ".token"

#: The organizer's Luma API key. Theirs, for someone else's service.
LUMA_KEY_SUFFIX = ".luma-key"


class CredentialError(Exception):
    """A secret file exists but cannot be read as one — wrong encoding, or an OS-level
    read failure. Carries a sentence written for the organizer, naming the file and the
    fix, so it never surfaces as a bare traceback at startup or a blank 500 per request."""


def secret_path(store_path: Path | str, suffix: str) -> Path:
    """Where a secret for ``store_path`` lives: beside it, same name plus ``suffix``.

    The suffix attaches to the filename, not to the path as text, so a ``store_path`` with
    a trailing separator resolves to the same file as one without.
    """
    path = Path(store_path)
    return path.with_name(path.name + suffix)


def restrict(path: Path) -> None:
    """SECURITY: narrow a file to its owner. Called on read as well as write, so a file
    a copy widened is fixed rather than trusted."""
    if sys.platform != "win32":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_secret(store_path: Path | str, suffix: str, value: str) -> Path:
    """Write a secret at as narrow a mode as the platform allows.

    SECURITY: write to a fresh 0600 temporary file and rename over the target; never write
    into the target itself. Writing in place leaves the value readable at the target's
    existing mode until it is narrowed. The rename also makes the replacement atomic.
    """
    path = secret_path(store_path, suffix)
    # `mkstemp` creates 0600 with O_EXCL, so the temporary file cannot be an existing
    # loose one. Same directory, so the rename stays on one filesystem.
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        restrict(temp)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return path


def read_secret(store_path: Path | str, suffix: str) -> str | None:
    """The secret, or ``None`` if there is none. An empty file reads as ``None``, so an
    interrupted write cannot become a credential the empty string satisfies."""
    path = secret_path(store_path, suffix)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        # A token an editor saved as UTF-16/ANSI, or a file the owner can no longer read.
        # Fail closed with a sentence, never a bypass.
        raise CredentialError(
            f"the secret file {path} could not be read (it is not UTF-8 text, or is unreadable). "
            "Delete it and it will be recreated: the session token regenerates, and the Luma key "
            "can be re-entered."
        ) from exc
    value = raw.strip()
    if not value:
        return None
    restrict(path)
    return value


__all__ = [
    "LUMA_KEY_SUFFIX",
    "TOKEN_SUFFIX",
    "CredentialError",
    "read_secret",
    "restrict",
    "secret_path",
    "write_secret",
]
