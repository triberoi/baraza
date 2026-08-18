"""Luma Calendar API client — the HTTP seam.

Fronts the public Luma API (https://api.lu.ma/public/v1). ``LumaClient`` is a Protocol,
so the mapping and write path test against a fake; the live ``HttpxLumaClient`` runs only
when a real API key is supplied.

Endpoints:
  GET /calendar/list-events                    → paginated event list
  GET /event/get?api_id=…                       → single event detail
  GET /event/get-guests?event_api_id=…          → paginated guest list
Auth: ``x-luma-api-key`` header. Pagination: cursor (``pagination_cursor`` →
``next_cursor``/``has_more``). Rate limit: 429 with no Retry-After → retry twice with a
1s back-off, then surface.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, TypeVar

import httpx

logger = logging.getLogger(__name__)

LUMA_BASE_URL = "https://api.lu.ma/public/v1"

# A cycling cursor with `has_more: true` must not spin forever. 1000 pages is far
# beyond any real calendar; past it, fail loud.
MAX_PAGES = 1000


def _parse_dt(value: Any) -> datetime | None:
    """ISO-8601 (Luma emits ``…Z``) → tz-aware UTC datetime; None/empty/unparseable → None.

    Never raises: one malformed timestamp must cost one record, not the whole run. Every
    field it parses is optional except an event's ``start_at``, whose caller skips that
    single event."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Platform-controlled text: truncated, and logged rather than raised.
        logger.warning("Luma: unparseable timestamp %r — treated as absent.", value[:64])
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass(frozen=True)
class LumaEvent:
    """A Luma event, parsed to the fields the connector consumes. ``end_at`` feeds
    the derived no-show; ``venue`` is the resolved address; ``tags`` are names."""

    api_id: str
    name: str
    start_at: datetime
    end_at: datetime | None
    capacity: int
    venue: str
    created_at: datetime | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class LumaGuest:
    api_id: str
    approval_status: str
    user_email: str
    user_name: str | None
    registered_at: datetime | None
    checked_in_at: datetime | None


def _event_from_api(d: dict[str, Any]) -> LumaEvent:
    geo = d.get("geo_address_info") or {}
    venue = geo.get("full_address") or geo.get("city_state") or ""
    tags = tuple(t["name"] for t in (d.get("tags") or []) if t.get("name"))
    start_at = _parse_dt(d.get("start_at"))
    if start_at is None:
        raise ValueError(f"Luma event {d.get('api_id')!r} has no parseable start_at")
    return LumaEvent(
        api_id=d["api_id"],
        name=d.get("name") or "",
        start_at=start_at,
        end_at=_parse_dt(d.get("end_at")),
        capacity=int(d["capacity"]) if d.get("capacity") is not None else 0,
        venue=venue,
        created_at=_parse_dt(d.get("created_at")),
        tags=tags,
    )


def _guest_from_api(d: dict[str, Any]) -> LumaGuest:
    return LumaGuest(
        api_id=d["api_id"],
        approval_status=str(d.get("approval_status") or ""),
        user_email=d.get("user_email") or "",
        user_name=d.get("user_name"),
        registered_at=_parse_dt(d.get("registered_at")),
        checked_in_at=_parse_dt(d.get("checked_in_at")),
    )


_T = TypeVar("_T")


def _map_entries(entries: list[dict[str, Any]], build: Callable[[dict[str, Any]], _T], kind: str) -> list[_T]:
    """Build one object per API entry, skipping any in a shape we cannot use.

    A missing required field is a defect in one record; raising would make it a defect in
    the whole run. Each skip is logged with the entry's id rather than swallowed."""
    built: list[_T] = []
    for entry in entries:
        try:
            built.append(build(entry))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            api_id = entry.get("api_id") if isinstance(entry, dict) else None
            logger.warning("Luma: skipping unusable %s %r (%s)", kind, api_id, exc.__class__.__name__)
    return built


class LumaClient(Protocol):
    """The seam the connector depends on (tests bind a fake)."""

    def probe(self) -> None: ...
    def list_events(self) -> list[LumaEvent]: ...
    def get_event(self, api_id: str) -> LumaEvent | None: ...
    def list_event_guests(self, event_api_id: str) -> list[LumaGuest]: ...


class HttpxLumaClient:
    """Live Luma client over httpx. Not exercised in CI, which has no key — the mapping
    and write path are tested through a fake."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = LUMA_BASE_URL,
        max_retries: int = 2,
        http: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"x-luma-api-key": api_key, "accept": "application/json"}
        self._max_retries = max_retries
        self._http = http or httpx.Client(timeout=30.0)

    def close(self) -> None:
        """Release the connection pool."""
        self._http.close()

    def __enter__(self) -> HttpxLumaClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        """The configured key, removed from anything on its way to a log.

        SECURITY: Luma echoes the submitted key in some error bodies, and those bodies are
        logged verbatim below.
        """
        key = self._headers.get("x-luma-api-key")
        return text.replace(key, "[redacted]") if key else text

    def _get_json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                res = self._http.get(f"{self._base}{path}", params=params, headers=self._headers)
            except httpx.HTTPError as exc:
                # Offline is the likeliest failure here, and an httpx error escaping
                # reaches the terminal as a traceback. The class name is kept because
                # "which kind of unreachable" is useful; the exception's own text is not,
                # since it can carry a resolved address.
                raise RuntimeError(
                    f"Luma is unreachable on {path} ({exc.__class__.__name__}) — check the network and try again"
                ) from exc
            if res.status_code == 429:
                if attempt < self._max_retries:
                    time.sleep(1.0)
                    continue
                # Rate-limiting says its own name here, in the loop. After the loop is
                # unreachable: on the last attempt the retry condition is False, so a 429
                # would fall through to the generic branch and report a bare code.
                attempts = self._max_retries + 1
                logger.warning("Luma API rate-limited %s after %d attempts", path, attempts)
                raise RuntimeError(f"Luma is rate-limiting this key — {attempts} attempts on {path}, all refused")
            if res.is_redirect:
                # The API never redirects, so a 3xx is something else answering — a
                # captive portal, typically. Not followed. The Location may carry the key
                # as a query parameter, so it is logged, never raised.
                logger.warning(
                    "Luma API %s on %s redirected to %s",
                    res.status_code,
                    path,
                    self._redact(res.headers.get("location", "")),
                )
                raise RuntimeError(f"Luma API {res.status_code} on {path} redirected instead of answering")
            if res.status_code >= 400:
                # Platform-controlled text stays in the log, never in the message a
                # caller may persist or display.
                logger.warning("Luma API %s on %s: %s", res.status_code, path, self._redact(res.text[:200]))
                raise RuntimeError(f"Luma API {res.status_code} on {path}")
            try:
                data: dict[str, Any] = res.json()
            except ValueError as exc:
                # A 2xx whose body is not JSON: a proxy login page, a WAF interstitial,
                # a truncated response. Body to the log on the 4xx branch's terms.
                logger.warning("Luma API 200 on %s with a non-JSON body: %s", path, self._redact(res.text[:200]))
                raise RuntimeError(f"Luma API returned a non-JSON body on {path}") from exc
            return data
        # Unreachable: every path through the loop returns or raises. Kept because the
        # type checker cannot see that, and labelled so nobody "fixes" it into a branch
        # that cannot fire.
        raise AssertionError(f"unreachable: the retry loop on {path} always returns or raises")

    def _paginate(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        """Walk the cursor to the end of the list.

        A short list must never be mistaken for a complete one: everything downstream
        reads a missing member as a member who left. So the two ambiguous pages are
        separated. One that claims more and supplies no cursor fails loud; one that says
        nothing either way falls back to the cursor, the only evidence on offer.
        """
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            page = self._get_json(path, {**params, **({"pagination_cursor": cursor} if cursor else {})})
            out.extend(page.get("entries") or [])
            has_more, cursor = page.get("has_more"), (page.get("next_cursor") or None)
            if has_more is None:
                if cursor is None:
                    return out
            elif not has_more:
                return out
            elif cursor is None:
                raise RuntimeError(
                    f"Luma API reported more pages on {path} but returned no cursor — "
                    "refusing to report a partial list as complete"
                )
        raise RuntimeError(f"Luma API exceeded {MAX_PAGES} pages on {path} — cursor loop or absurd corpus")

    def probe(self) -> None:
        """The cheapest authenticated read: one event-list page of one entry, so a bad key
        fails without fetching a whole calendar."""
        self._get_json("/calendar/list-events", {"pagination_limit": "1"})

    def list_events(self) -> list[LumaEvent]:
        return _map_entries(self._paginate("/calendar/list-events", {}), _event_from_api, "event")

    def get_event(self, api_id: str) -> LumaEvent | None:
        try:
            result = self._get_json("/event/get", {"api_id": api_id})
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise
        event = result.get("event")
        return _event_from_api(event) if event else None

    def list_event_guests(self, event_api_id: str) -> list[LumaGuest]:
        entries = self._paginate("/event/get-guests", {"event_api_id": event_api_id})
        return _map_entries(entries, _guest_from_api, "guest")


__all__ = ["HttpxLumaClient", "LumaClient", "LumaEvent", "LumaGuest", "LUMA_BASE_URL"]
