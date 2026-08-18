"""The Luma Calendar API client (``baraza.ingest.client``).

Binds a fake ``httpx`` transport rather than a fake client, so the pagination and parsing
the seam exists to hide are actually exercised.

The two failure modes under test are the ones that lie rather than crash: one malformed
timestamp costing every good record beside it, and a page that stops supplying a cursor
being read as the last page — a truncated calendar returned as a complete one, in which
every missing member reads as a member who left.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from baraza.ingest.client import HttpxLumaClient

BASE = "https://api.test/v1"


def _client(pages: list[dict[str, Any]], *, path_filter: str | None = None) -> HttpxLumaClient:
    """A client whose transport serves ``pages`` in order, one per request."""
    served: list[dict[str, Any]] = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if path_filter is not None and path_filter not in request.url.path:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=served.pop(0) if served else {"entries": []})

    return HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))


def _event(api_id: str, **overrides: Any) -> dict[str, Any]:
    return {"api_id": api_id, "name": api_id, "start_at": "2026-05-01T18:00:00Z", **overrides}


def _guest(api_id: str, **overrides: Any) -> dict[str, Any]:
    return {"api_id": api_id, "approval_status": "approved", "user_email": f"{api_id}@x.com", **overrides}


# --- one bad record must not cost the run ----------------------------
def test_an_unparseable_optional_timestamp_does_not_abort_the_run() -> None:
    """``end_at`` is optional, and it used to raise straight out of ``_parse_dt``. An
    event with a garbled one took the whole calendar with it."""
    client = _client([{"entries": [_event("evt-1", end_at="not-a-date"), _event("evt-2")], "has_more": False}])
    events = client.list_events()
    assert [e.api_id for e in events] == ["evt-1", "evt-2"]
    assert events[0].end_at is None  # absent, not fatal


def test_an_event_missing_its_required_start_is_skipped_and_the_rest_survive() -> None:
    """``start_at`` genuinely IS required — an event without one cannot answer "has this
    happened", which is what the derived no-show rests on. So the record is dropped,
    not defaulted. The fix is that it drops ALONE: the good events on either side of it
    used to be lost with it."""
    client = _client(
        [
            {
                "entries": [_event("evt-good-1"), _event("evt-bad", start_at="tomorrow-ish"), _event("evt-good-2")],
                "has_more": False,
            }
        ]
    )
    assert [e.api_id for e in client.list_events()] == ["evt-good-1", "evt-good-2"]


def test_a_guest_the_platform_sends_in_an_unusable_shape_is_skipped_alone() -> None:
    """The same property on the guest list, where losing the run is worse: a guest list
    is the attendance record for one event, so an aborted fetch reads as an empty event.
    """
    client = _client([{"entries": [_guest("g1"), {"no_api_id": True}, _guest("g2")], "has_more": False}])
    assert [g.api_id for g in client.list_event_guests("evt-1")] == ["g1", "g2"]


def test_a_bad_timestamp_is_never_echoed_whole_into_a_message() -> None:
    """The value is platform-controlled text and the client's exception messages are
    persisted to the connector status card, so a bad timestamp is logged (truncated) and
    never raised — the card must not become a mirror for whatever a host sends."""
    client = _client([{"entries": [_event("evt-1", end_at="X" * 5000)], "has_more": False}])
    events = client.list_events()  # no raise
    assert events[0].end_at is None


# --- a short list must never pass as a complete one ------------------
def test_pagination_follows_the_cursor_to_the_end() -> None:
    client = _client(
        [
            {"entries": [_event("evt-1")], "has_more": True, "next_cursor": "c1"},
            {"entries": [_event("evt-2")], "has_more": True, "next_cursor": "c2"},
            {"entries": [_event("evt-3")], "has_more": False},
        ]
    )
    assert [e.api_id for e in client.list_events()] == ["evt-1", "evt-2", "evt-3"]


def test_a_page_claiming_more_with_no_cursor_fails_loud() -> None:
    """This is the defect exactly. ``next_cursor if has_more else None`` collapsed to
    None here, the loop returned, and the caller got page one presented as the whole
    calendar. There is no correct value to invent, so the only honest outcome is to
    refuse — a sync that reports partial data as complete deletes the difference."""
    client = _client([{"entries": [_event("evt-1")], "has_more": True}])  # no next_cursor
    with pytest.raises(RuntimeError, match="partial list as complete"):
        client.list_events()


def test_an_empty_cursor_string_counts_as_no_cursor() -> None:
    """``""`` is falsy and was swallowed by the same expression. Spelt out because a
    JSON API returning an empty string where it means null is ordinary."""
    client = _client([{"entries": [_event("evt-1")], "has_more": True, "next_cursor": ""}])
    with pytest.raises(RuntimeError, match="partial list as complete"):
        client.list_events()


def test_a_page_with_no_has_more_marker_falls_back_to_the_cursor() -> None:
    """The other half of it. A missing marker is not evidence of the end, and
    reading it as one truncates silently. The cursor is the only evidence on offer, so
    it decides."""
    client = _client(
        [
            {"entries": [_event("evt-1")], "next_cursor": "c1"},  # no has_more at all
            {"entries": [_event("evt-2")]},  # no marker, no cursor → genuinely done
        ]
    )
    assert [e.api_id for e in client.list_events()] == ["evt-1", "evt-2"]


def test_a_cursor_that_never_advances_still_stops() -> None:
    """The pre-existing MAX_PAGES backstop, asserted rather than assumed — the fix above
    makes the client MORE willing to keep paging, so the loop guard now matters more
    than it did."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entries": [_event("evt-1")], "has_more": True, "next_cursor": "same"})

    client = HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="cursor loop or absurd corpus"):
        client.list_events()


# --- the seam's own behavior -----------------------------------------------------
def test_an_error_body_stays_out_of_the_exception_message() -> None:
    """The message can be persisted and shown to a user by whatever calls this.
    Platform-controlled body text belongs in the log and nowhere else."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="SECRET-ish internal detail")

    client = HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError) as excinfo:
        client.list_events()
    assert "SECRET-ish" not in str(excinfo.value)
    assert "500" in str(excinfo.value)


def test_the_api_key_travels_as_a_header_and_never_in_the_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"entries": [], "has_more": False})

    client = HttpxLumaClient("secret-key", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.probe()
    assert seen[0].headers["x-luma-api-key"] == "secret-key"
    assert "secret-key" not in str(seen[0].url)


def test_get_event_maps_404_to_none_and_re_raises_anything_else() -> None:
    codes = iter([404, 500])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(next(codes), text="")

    client = HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_event("evt-gone") is None
    with pytest.raises(RuntimeError):
        client.get_event("evt-boom")


# --- what a hostile or broken network puts on the wire ---
#
# Every case below reached the organizer as a raw traceback, or as the JSON parser's
# complaint about the parser's own problem.


def _raising_client(exc: Exception) -> HttpxLumaClient:
    def handler(_: httpx.Request) -> httpx.Response:
        raise exc

    return HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(handler)))


def _responding_client(response: httpx.Response) -> HttpxLumaClient:
    return HttpxLumaClient(
        "k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(lambda _: response))
    )


@pytest.mark.parametrize(
    "body",
    [
        "<html><body>Access denied by proxy</body></html>",  # a captive portal or WAF
        '{"entries": [{"api_id": "ev-1"',  # a truncated body
        "",  # a 200 with nothing in it
    ],
)
def test_a_two_hundred_that_is_not_json_is_a_sentence_not_a_parser_error(body: str) -> None:
    """A proxy login page, a WAF interstitial or a cut connection all arrive as a 2xx
    whose body is not JSON. `res.json()` was called unguarded, so the organizer met
    `json.decoder.JSONDecodeError` — the parser's words about the parser's problem."""
    client = _responding_client(httpx.Response(200, text=body))
    with pytest.raises(RuntimeError) as caught:
        client.probe()
    assert "non-JSON" in str(caught.value)


def test_a_redirect_is_a_dead_end_rather_than_something_to_parse() -> None:
    """3xx fell through the `>= 400` branch and was handed to the JSON parser. A captive
    portal answering 302 is the ordinary way this happens."""
    client = _responding_client(httpx.Response(302, headers={"location": "https://portal.test/login"}))
    with pytest.raises(RuntimeError) as caught:
        client.probe()
    assert "302" in str(caught.value)


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("getaddrinfo failed"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_being_unable_to_reach_luma_at_all_is_a_sentence(exc: Exception) -> None:
    """Offline is the single most likely failure of `baraza import --luma`, and it was the
    one with no message: `httpx.HTTPError` propagated out of `_get_json` untouched."""
    with pytest.raises(RuntimeError) as caught:
        _raising_client(exc).probe()
    assert "unreachable" in str(caught.value).lower()


def test_no_failure_message_ever_carries_the_body_or_the_key() -> None:
    """The message is the one a caller persists and renders, so it carries no platform
    Data Sources card, so platform-controlled text must not ride in it — and neither must
    the credential. Asserted as a property over every failure shape rather than per case,
    because the next failure mode added is the one a per-case list would miss."""
    secret = "secret-DEADBEEF-do-not-leak"
    marker = "PROXY-LOGIN-PAGE-CONTENT"
    failures = [
        _responding_client(httpx.Response(200, text=f"<html>{marker} {secret}</html>")),
        _responding_client(httpx.Response(401, text=f"bad key {secret} {marker}")),
        _responding_client(httpx.Response(302, headers={"location": f"https://portal.test/?k={secret}"})),
    ]
    for client in failures:
        client._headers["x-luma-api-key"] = secret  # the key this client is configured with
        with pytest.raises(RuntimeError) as caught:
            client.probe()
        assert secret not in str(caught.value)
        assert marker not in str(caught.value)


def test_the_client_can_be_closed_and_used_as_a_context_manager() -> None:
    """`ConnectorHttp` in the main product has `close()` and both context-manager methods;
    this one owned a connection pool with no way to release it."""
    ok = httpx.Response(200, json={"entries": [], "has_more": False})
    client = HttpxLumaClient("k", base_url=BASE, http=httpx.Client(transport=httpx.MockTransport(lambda _: ok)))
    with client as entered:
        assert entered is client
    assert client._http.is_closed
