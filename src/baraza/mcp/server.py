"""The MCP server — a registration shim. Every answer comes from
:mod:`baraza.mcp.tools`, which imports no protocol.

The ``mcp`` dependency is an optional extra (``pip install 'baraza[mcp]'``), so its
import is deferred to :func:`build` and its absence is an instruction, not a traceback.

SECURITY: read-only, and stdio only. An HTTP transport would need the loopback socket,
the Host and Origin guards and the session token all over again.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from baraza import __version__
from baraza.mcp.tools import TOOLS
from baraza.store import connect

INSTALL_HINT = "The MCP server needs its optional dependency: pip install 'baraza[mcp]'"

#: SECURITY: operator parameters, kept out of every tool's schema. A client that could
#: set ``store_path`` could read any SQLite file the process can open; one that could set
#: ``now`` could report numbers that disagree with the screens.
OPERATOR_SEAMS = frozenset({"store_path", "now"})


def build(store_path: Path | str) -> Any:
    """A ``FastMCP`` server exposing :data:`baraza.mcp.tools.TOOLS` over ``store_path``.

    Returns ``Any`` because annotating ``FastMCP`` would need the import at module scope.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised in CI, where the extra installs
        raise ImportError(INSTALL_HINT) from exc

    server = FastMCP("baraza")
    # Without this, `serverInfo.version` reports the `mcp` library's version, not ours.
    # Reached through `_mcp_server` because FastMCP 1.x has no setter; the tests pin the
    # result, so a later release moving the attribute fails there rather than on the wire.
    server._mcp_server.version = __version__
    for tool in TOOLS:
        server.add_tool(_bound(tool, store_path), name=tool.__name__, description=_summary(tool))
    return server


def _bound(tool: Any, store_path: Path | str) -> Any:
    """Bind the store to a tool, keeping every argument that is the client's.

    FastMCP builds the JSON schema from the callable's signature, so ``__signature__``
    must be set explicitly — a bare ``**kwargs`` wrapper publishes an empty schema and the
    client has nowhere to put an argument.

    Do not set ``__wrapped__``: ``inspect.signature`` follows it back to the unbound
    function and puts the operator seams back in the schema.
    """
    signature = inspect.signature(tool)
    exposed = [p for name, p in signature.parameters.items() if name not in OPERATOR_SEAMS]

    def call(*args: Any, **kwargs: Any) -> Any:
        return tool(store_path, *args, **kwargs)

    call.__name__ = tool.__name__
    call.__doc__ = tool.__doc__
    call.__signature__ = signature.replace(parameters=exposed)  # type: ignore[attr-defined]
    return call


def _summary(tool: Any) -> str:
    """The first paragraph of the tool's docstring — what the model is told it does."""
    doc = (tool.__doc__ or "").strip()
    return " ".join(doc.split("\n\n")[0].split())


def serve_stdio(store_path: Path | str) -> None:
    """Run the server on stdio until the client disconnects. Blocks.

    The store is opened before the transport starts, so an unusable ``--store`` reaches the
    operator rather than the model. ``create=True`` matches ``serve``: a first run has no
    store yet.
    """
    connect(store_path, create=True).close()
    build(store_path).run(transport="stdio")


__all__ = ["INSTALL_HINT", "build", "serve_stdio"]
