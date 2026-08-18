"""The MCP server: the answers the screens give, to a model instead of a page.

Read-only. ``tools`` imports no protocol; ``server`` is the shim and needs the optional
``mcp`` dependency.
"""

from __future__ import annotations

from baraza.mcp.tools import MAX_PEOPLE, TOOLS, overview, people, person, retention

__all__ = ["MAX_PEOPLE", "TOOLS", "overview", "people", "person", "retention"]
