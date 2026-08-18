"""Tests for the Luma event-metadata resolver (``baraza.ingest.resolver``).

Pure (no network): the HTTP path is driven through ``httpx.MockTransport``; the
retry policy is driven through a counting fake. No DB needed.
"""

from datetime import UTC, datetime

import httpx
import pytest

from baraza.ingest.resolver import (
    LumaEventMetadata,
    LumaResolveFailure,
    resolve_luma_event,
    resolve_with_retry,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _event_body(**over) -> dict:
    event = {
        "name": "AI Workshop",
        "start_at": "2026-05-01T18:00:00Z",
        "end_at": "2026-05-01T20:00:00Z",
        "timezone": "America/Los_Angeles",
        "cover_url": "https://img/x.png",
        **over,
    }
    return {"event": event}


# --- resolved -------------------------------------------------------------------
def test_resolves_event_metadata() -> None:
    with _client(lambda req: httpx.Response(200, json=_event_body())) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaEventMetadata)
    assert r.name == "AI Workshop"
    assert r.start_at == datetime(2026, 5, 1, 18, tzinfo=UTC)
    assert r.end_at == datetime(2026, 5, 1, 20, tzinfo=UTC)
    assert r.timezone == "America/Los_Angeles"


def test_passes_event_api_id_param() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["id"] = req.url.params.get("event_api_id")
        return httpx.Response(200, json=_event_body())

    with _client(handler) as c:
        resolve_luma_event("evt-XYZ", http=c)
    assert seen["id"] == "evt-XYZ"


# --- failure modes --------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "reason"),
    [(404, "not_found"), (429, "rate_limited"), (500, "network_error"), (403, "network_error")],
)
def test_http_status_failures(status, reason) -> None:
    with _client(lambda req: httpx.Response(status)) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaResolveFailure) and r.reason == reason


def test_malformed_json_body() -> None:
    with _client(lambda req: httpx.Response(200, text="<html>not json</html>")) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaResolveFailure) and r.reason == "malformed_response"


@pytest.mark.parametrize(
    "body", [{"notevent": 1}, {"event": {"start_at": "2026-05-01T18:00:00Z"}}, {"event": {"name": "X"}}]
)
def test_missing_fields_are_malformed(body) -> None:
    with _client(lambda req: httpx.Response(200, json=body)) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaResolveFailure) and r.reason == "malformed_response"


def test_connect_error_is_endpoint_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    with _client(handler) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaResolveFailure) and r.reason == "endpoint_unavailable"


def test_timeout_is_network_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    with _client(handler) as c:
        r = resolve_luma_event("evt-1", http=c)
    assert isinstance(r, LumaResolveFailure) and r.reason == "network_error"


# --- retry policy ---------------------------------------------------------------
def test_retry_once_on_transient_then_succeeds() -> None:
    calls = {"n": 0}
    ok = LumaEventMetadata("resolved", "evt-1", "X", datetime(2026, 5, 1, tzinfo=UTC), None, None, None)

    def resolve(_id: str):
        calls["n"] += 1
        return LumaResolveFailure("unresolved", "evt-1", "network_error", "blip") if calls["n"] == 1 else ok

    slept = []
    r = resolve_with_retry("evt-1", resolve, sleep=slept.append)
    assert r is ok and calls["n"] == 2 and slept == [0.5]


def test_definite_failure_does_not_retry() -> None:
    calls = {"n": 0}

    def resolve(_id: str):
        calls["n"] += 1
        return LumaResolveFailure("unresolved", "evt-1", "not_found", "gone")

    slept = []
    r = resolve_with_retry("evt-1", resolve, sleep=slept.append)
    assert isinstance(r, LumaResolveFailure) and calls["n"] == 1 and slept == []
