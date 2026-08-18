"""Fills a guest-list CSV's missing event name and times from Luma's unauthenticated
``event/get`` endpoint, so the free path works without an API key.

Never raises: the result is ``LumaEventMetadata`` or ``LumaResolveFailure`` with a
``reason`` the caller uses to choose retry or manual entry.

The endpoint is undocumented, so Luma could change or restrict it. Correctness never
depends on it — an unresolved result routes to manual entry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from baraza import __version__

LUMA_EVENT_GET_URL = "https://api.lu.ma/event/get"
_DEFAULT_TIMEOUT_S = 5.0

LumaResolveFailureReason = Literal[
    "not_found", "rate_limited", "malformed_response", "network_error", "endpoint_unavailable"
]


@dataclass(frozen=True)
class LumaEventMetadata:
    kind: Literal["resolved"]
    luma_event_id: str
    name: str
    start_at: datetime
    end_at: datetime | None
    timezone: str | None
    cover_url: str | None


@dataclass(frozen=True)
class LumaResolveFailure:
    kind: Literal["unresolved"]
    luma_event_id: str
    reason: LumaResolveFailureReason
    message: str


ResolveResult = LumaEventMetadata | LumaResolveFailure
ResolveFn = Callable[[str], ResolveResult]


def _fail(luma_event_id: str, reason: LumaResolveFailureReason, message: str) -> LumaResolveFailure:
    return LumaResolveFailure(kind="unresolved", luma_event_id=luma_event_id, reason=reason, message=message)


def _parse_dt(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def resolve_luma_event(
    luma_event_id: str,
    *,
    http: httpx.Client | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> ResolveResult:
    """Resolve one Luma event's metadata. Never raises."""
    client = http or httpx.Client(timeout=timeout)
    headers = {"user-agent": f"baraza/{__version__}", "accept": "application/json"}
    try:
        resp = client.get(LUMA_EVENT_GET_URL, params={"event_api_id": luma_event_id}, headers=headers)
    except httpx.TimeoutException:
        return _fail(luma_event_id, "network_error", f"Timed out resolving {luma_event_id}.")
    except httpx.ConnectError as e:
        return _fail(luma_event_id, "endpoint_unavailable", f"Luma endpoint unreachable for {luma_event_id}: {e}")
    except httpx.HTTPError as e:
        return _fail(luma_event_id, "network_error", f"Fetch failed for {luma_event_id}: {e}")
    finally:
        if http is None:
            client.close()

    if resp.status_code == 404:
        return _fail(luma_event_id, "not_found", f"Luma returned 404 for {luma_event_id}.")
    if resp.status_code == 429:
        return _fail(luma_event_id, "rate_limited", f"Luma rate-limited the request for {luma_event_id}.")
    if resp.status_code >= 400:
        # 5xx and other 4xx → transient by default, so the retry policy can fire.
        return _fail(luma_event_id, "network_error", f"Luma returned {resp.status_code} for {luma_event_id}.")

    try:
        body = resp.json()
    except ValueError:
        return _fail(
            luma_event_id, "malformed_response", f"Luma returned 200 but the body wasn't valid JSON ({luma_event_id})."
        )

    event = body.get("event") if isinstance(body, dict) else None
    if not isinstance(event, dict):
        return _fail(
            luma_event_id, "malformed_response", f"Luma response missing the `event` object ({luma_event_id})."
        )

    name = event.get("name")
    if not isinstance(name, str) or not name:
        return _fail(luma_event_id, "malformed_response", f"Luma event {luma_event_id} response is missing `name`.")
    start_at = _parse_dt(event.get("start_at"))
    if start_at is None:
        return _fail(
            luma_event_id, "malformed_response", f"Luma event {luma_event_id} `start_at` is missing or unparseable."
        )

    tz = event.get("timezone")
    cover = event.get("cover_url")
    return LumaEventMetadata(
        kind="resolved",
        luma_event_id=luma_event_id,
        name=name,
        start_at=start_at,
        end_at=_parse_dt(event.get("end_at")),
        timezone=tz if isinstance(tz, str) and tz else None,
        cover_url=cover if isinstance(cover, str) and cover else None,
    )


def resolve_with_retry(
    luma_event_id: str,
    resolve: ResolveFn,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> ResolveResult:
    """One 500ms retry on a transient failure. Definite failures surface immediately."""
    first = resolve(luma_event_id)
    if first.kind == "resolved" or first.reason not in ("network_error", "endpoint_unavailable"):
        return first
    sleep(0.5)
    return resolve(luma_event_id)


__all__ = [
    "LUMA_EVENT_GET_URL",
    "LumaEventMetadata",
    "LumaResolveFailure",
    "LumaResolveFailureReason",
    "ResolveFn",
    "ResolveResult",
    "resolve_luma_event",
    "resolve_with_retry",
]
