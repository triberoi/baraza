"""Reading a Luma calendar: API client, event-metadata resolver, guest-list CSV parser
and combiner, and the two shared mappings. Talks to Luma and to no database.
"""

from __future__ import annotations

from baraza.ingest.client import (
    LUMA_BASE_URL,
    HttpxLumaClient,
    LumaClient,
    LumaEvent,
    LumaGuest,
)
from baraza.ingest.guest_list import (
    FEEDBACK_MAX_CHARS,
    CombinerResult,
    LumaEventGuestList,
    LumaGuestRow,
    LumaParseError,
    ParsedLumaFile,
    ResolvedEvent,
    combine_luma_guest_lists,
    infer_title_from_filename,
    parse_luma_guest_list,
)
from baraza.ingest.mappers import (
    GUEST_STATUSES,
    email_to_member_id,
    resolve_guest_status,
)
from baraza.ingest.resolver import (
    LUMA_EVENT_GET_URL,
    LumaEventMetadata,
    LumaResolveFailure,
    LumaResolveFailureReason,
    ResolveFn,
    ResolveResult,
    resolve_luma_event,
    resolve_with_retry,
)

__all__ = [
    "FEEDBACK_MAX_CHARS",
    "GUEST_STATUSES",
    "LUMA_BASE_URL",
    "LUMA_EVENT_GET_URL",
    "CombinerResult",
    "HttpxLumaClient",
    "LumaClient",
    "LumaEvent",
    "LumaEventGuestList",
    "LumaEventMetadata",
    "LumaGuest",
    "LumaGuestRow",
    "LumaParseError",
    "LumaResolveFailure",
    "LumaResolveFailureReason",
    "ParsedLumaFile",
    "ResolveFn",
    "ResolveResult",
    "ResolvedEvent",
    "combine_luma_guest_lists",
    "email_to_member_id",
    "infer_title_from_filename",
    "parse_luma_guest_list",
    "resolve_guest_status",
    "resolve_luma_event",
    "resolve_with_retry",
]
