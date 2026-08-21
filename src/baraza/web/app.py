"""The JSON API the five screens read.

Thin by design: every endpoint opens the store, projects it onto the analytics input
pair, calls one function from :mod:`baraza.analytics`, and serializes. No arithmetic
lives here, so a wrong number is wrong in analytics, where its tests are.

Nothing is cached or precomputed. A laptop calendar is small and the roll-ups are cheap,
so every request recomputes from the store and a moved threshold takes effect on the next
load, with no invalidation to get wrong. This is why :mod:`baraza.store` holds facts and
not conclusions.

``now`` is supplied here, at the edge, injected as a callable so tests can pin it.
Analytics stays a function of its arguments; this is the only module allowed to know what
time it is.

One short-lived connection per request. SQLite objects to a connection used from a thread
other than the one that made it, and the server is threaded. On a 50-event store that
costs 0.56 ms to open and close against 3.3 ms to read the whole calendar, and a read
takes no write lock.

Field names are snake_case, matching the Python that produces them. The only consumer is
this package's own UI.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from baraza.analytics import ATTENDANCE_EVIDENCE, ScoredMember, Thresholds, analyze_members
from baraza.analytics.retention import retention_grid
from baraza.analytics.views import event_attendance, event_cohorts, lifecycle_mix, member_timeline
from baraza.credentials import LUMA_KEY_SUFFIX, TOKEN_SUFFIX, CredentialError, read_secret, write_secret
from baraza.importer import import_folder, import_luma_api
from baraza.ingest import HttpxLumaClient, LumaClient, ResolveFn, resolve_luma_event
from baraza.store import Store, StoredEvent, StoredMember, StoreError, connect
from baraza.web.token import ensure_token
from baraza.web.ui import API_PREFIX, mount_ui

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
LumaClientFactory = Callable[[str], LumaClient]

#: The only hostnames a request may claim to have been sent to. Binding to loopback
#: stops packets from other machines; it does **not** stop the organizer's own browser
#: being told by a hostile page that ``evil.example`` lives at 127.0.0.1 and then
#: treating this server as same-origin. That is DNS rebinding, and the whole roster —
#: names and email addresses, behind no login by design — is what it reads.
#:
#: The check is one line because the attack needs a Host header naming the attacker's
#: domain: a browser sends the name the user typed, not the address it resolved to.
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

#: Methods that change something. They get the stricter half of the origin check —
#: see :func:`_origin_refusal` for why the two halves differ.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Where the UI sends the session token. A query parameter is accepted as well, because
#: the first request a browser makes is a navigation and cannot carry a header.
TOKEN_HEADER = "x-baraza-token"
TOKEN_PARAM = "token"

#: The token is required on ``/api`` and nowhere else. Everything outside it is the
#: static UI, which contains no data — gating it would only stop the browser fetching
#: the page that is about to present the token.
#:
#: Defined in :mod:`baraza.web.ui` and re-exported here rather than written twice: the
#: UI's catch-all must refuse exactly what this claims.
GUARDED_PREFIX = API_PREFIX


def _utcnow() -> datetime:
    return datetime.now(UTC)


def hostname_of(header: str) -> str:
    """The name out of a ``Host`` header, port dropped.

    A function rather than an ``rsplit`` because bracketed IPv6 has no port: splitting
    ``[::1]`` on the last colon yields ``[:``, which matches nothing in
    :data:`ALLOWED_HOSTS`.

    Lowercased to match the ``Origin`` half, which gets it free from ``urlsplit``.
    Hostnames are case-insensitive, so ``Host: LOCALHOST`` is legal and must not be
    refused by one half of a guard the other half admits.
    """
    if header.startswith("["):
        closed = header.find("]")
        return (header[: closed + 1] if closed != -1 else header).lower()
    return (header.rsplit(":", 1)[0] if ":" in header else header).lower()


def _origin_refusal(method: str, origin: str | None) -> str | None:
    """SECURITY: why this request's ``Origin`` is unacceptable, or ``None`` if it is fine.

    This is CSRF, and the ``Host`` check does not cover it. A page on ``evil.example`` can
    ``fetch`` 127.0.0.1 directly — the ``Host`` header genuinely says ours, so the
    rebinding guard passes it. The browser withholds the response, never the side effect,
    and a ``POST`` with ``Content-Type: text/plain`` is a simple request that never
    preflights. Without this, any page the organizer visits could make the tool import a
    folder of the attacker's choosing.

    Two halves: any foreign ``Origin`` is refused, and an unsafe method with NO ``Origin``
    is refused too, since browsers always send one on a non-GET. A safe method with none is
    an ordinary same-origin GET. Scripting goes through the CLI, not the socket.
    """
    if origin is not None:
        parsed = urlsplit(origin)
        allowed = parsed.scheme in ("http", "https") and (parsed.hostname or "") in ALLOWED_HOSTS
        if not allowed:
            return "this request came from another site"
    elif method.upper() in UNSAFE_METHODS:
        return "a request that changes something must say where it came from"
    return None


def _token_matches(request: Request, expected: str) -> bool:
    """SECURITY: whether this request carries the session token.

    Header first, query parameter second — the browser's first contact is a navigation,
    which cannot send a header, so the token must arrive in the URL once.

    Compare with :func:`secrets.compare_digest`, and compare BYTES: a local attacker can
    retry as fast as the machine allows, and ``compare_digest`` on ``str`` raises
    ``TypeError`` unless both sides are ASCII. The presented side is whatever a caller
    sends and the expected side is a file an organizer can edit, so either can be
    non-ASCII — which must not match, not 500.
    """
    presented = request.headers.get(TOKEN_HEADER) or request.query_params.get(TOKEN_PARAM) or ""
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


class ThresholdsBody(BaseModel):
    """The organizer's four boundaries.

    Bounds are declared here as well as in :class:`Thresholds`, so a bad value is a 422
    naming the field rather than a 500 from deeper in. Cross-field rules stay in
    :class:`Thresholds` — two copies would be two rules to keep in step.
    """

    regular_min_events: int = Field(ge=2)
    champion_min_events: int = Field(ge=2)
    champion_min_rate: float = Field(gt=0.0, le=1.0)
    # Bounded at both ends. `timedelta` cannot hold more than this many days, and
    # `lifecycle_label` builds one from it — so an unbounded value here was accepted,
    # persisted, and then raised on every later read of the store.
    lapsed_after_days: int = Field(ge=1, le=Thresholds.MAX_LAPSED_AFTER_DAYS)


class ImportFolderBody(BaseModel):
    """A folder on the organizer's own machine. Any path they can read is fair game —
    it is their computer, and the server is theirs and reachable only from it. There is
    nothing to sandbox them away from.

    Except the empty one. ``Path("")`` is ``.``, so an untouched box scans whatever
    directory the server started in, finds no Luma exports, and reports a successful
    import of nothing. ``min_length`` makes it a 422 naming the field.
    """

    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        """Whitespace is empty too. ``min_length`` alone lets ``" "`` through, and
        ``Path(" ")`` is no more a folder the organizer meant than ``Path("")`` is."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("give the folder holding your Luma exports")
        return stripped


class ImportLumaBody(BaseModel):
    """An optional Luma API key.

    Optional because after the first time there is one on disk: the organizer pastes it
    once and every later refresh is a button. Supplying one replaces what is stored —
    but only after it has been proved to work, so a typo cannot lock them out of the
    key that did.
    """

    api_key: str | None = None


class AttendanceEvidenceBody(BaseModel):
    """What an event measured, as the organizer declares it.

    Its own body and route rather than a field on :class:`EventBody`: merging them would
    let an import carry a default for a field no export contains.
    """

    attendance_evidence: str

    @field_validator("attendance_evidence")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in ATTENDANCE_EVIDENCE:
            raise ValueError(f"expected one of {sorted(ATTENDANCE_EVIDENCE)}")
        return value


class EventBody(BaseModel):
    """The name and date the organizer types for an event the resolver could not find.

    Writing it creates the event row, which is what the next import over the same
    folder looks up first — so their answer is authoritative and a later lookup cannot
    quietly overwrite it.
    """

    title: str = Field(min_length=1)
    starts_at: datetime
    ends_at: datetime | None = None


def _member_row(member: ScoredMember, people: dict[str, StoredMember]) -> dict[str, Any]:
    """One roster row: what the member did, plus who they are.

    The identity half comes from the store and the judgment half from analytics,
    joined on ``member_id`` — which is the whole reason the projection in
:meth:`Store.calendar` can withhold the email without costing the UI anything.
    """
    known = people.get(member.member_id)
    facts = member.facts
    return {
        "member_id": member.member_id,
        "name": known.display_name if known else None,
        "email": known.email if known else None,
        "score": member.score,
        "lifecycle": member.lifecycle,
        "events_attended": facts.events_attended,
        # Always sent, so a roster row can say how much of its total was measured.
        "unmeasured_attendances": facts.unmeasured_attendances,
        "no_shows": facts.no_shows,
        "opportunities": facts.opportunities,
        "attendance_rate": facts.attendance_rate,
        "first_seen_at": facts.first_seen_at,
        "last_attended_at": facts.last_attended_at,
    }


def create_app(
    store_path: Path | str,
    *,
    clock: Clock = _utcnow,
    resolve: ResolveFn | None = resolve_luma_event,
    luma_client: LumaClientFactory = HttpxLumaClient,
) -> FastAPI:
    """Build the API over the artifact at ``store_path``.

    Both seams exist for the same reason: the two things this module cannot control.
    ``clock`` is injected rather than read, so a test can ask what the roster looked
    like on a chosen day without waiting for one. ``resolve`` is injected because the
    real one is a live call to Luma's undocumented event endpoint — a suite that
    reached it would be testing Luma's uptime, and would fail on a train. Pass ``None``
    to run the import entirely offline.

    ``luma_client`` is the third seam and exists for the same reason as ``resolve``:
    the real one talks to Luma over the network, and a suite that reached it would be
    testing someone else's service.

    SECURITY: the session token is read from (or written to) the file beside the store
    and published as ``app.state.token``. There is deliberately no way to construct an app
    without one — an "auth off" switch is a switch somebody ships enabled.
    """
    # Open the store first, before `ensure_token` writes a file beside it. Otherwise the
    # first open happens on the first request: pointing `--store` at a document prints a
    # working link, leaves a stray token file next to that document, and fails on every
    # screen. `create=True` because a first run legitimately has no store yet.
    connect(store_path, create=True).close()

    app = FastAPI(title="baraza", docs_url=None, redoc_url=None)

    @app.exception_handler(StoreError)
    async def _store_unreadable(_request: Request, exc: StoreError) -> JSONResponse:
        """Every `StoreError` carries a sentence written for the organizer. Without this
        handler they are thrown away and a moved store becomes a bare 500 with a
        `text/plain` body, which the UI shows as "The request failed (500)."

        503, not 500: the store is a file the organizer can put back.
        """
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(CredentialError)
    async def _credential_unreadable(_request: Request, exc: CredentialError) -> JSONResponse:
        """The per-request token read raises this when the token file is not UTF-8 or is
        unreadable. 503, not 500: a file the owner can fix, and the message names it.
        """
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    app.state.token = ensure_token(store_path)
    router = APIRouter(prefix="/api")

    @app.middleware("http")
    async def only_from_this_machine(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """SECURITY: the whole browser boundary, in two checks answering different attacks.

        ``Host`` refuses a request arriving under the ATTACKER's name — DNS rebinding. The
        port is ignored; a missing header is refused, since HTTP/1.1 requires one.

        ``Origin`` refuses one arriving under OUR name from someone else's page — CSRF,
        which the ``Host`` check structurally cannot see.

        Middleware, not a dependency, so a route added later cannot forget to ask.
        """
        if hostname_of(request.headers.get("host", "")) not in ALLOWED_HOSTS:
            return JSONResponse(
                status_code=421,
                content={"detail": "baraza only answers requests addressed to this machine."},
            )
        refusal = _origin_refusal(request.method, request.headers.get("origin"))
        if refusal is not None:
            return JSONResponse(status_code=403, content={"detail": f"baraza refused this request: {refusal}."})
        # Re-read per request, never a copy taken at startup. The file IS the boundary —
        # the only thing another account on this machine cannot read — so what it says now
        # is what the socket must accept now. Against a startup copy, deleting the file
        # revokes nothing until the next `baraza serve`.
        #
        # Read only for a guarded request — a small file read on loopback, for one person —
        # so a corrupt token file does not blank the UI shell, only the `/api` calls the
        # shell then makes, which surface the sentence. `read_secret` also re-applies the
        # file's permissions on read, and a deleted file leaves nothing to match — the
        # revocation.
        if request.url.path.startswith(GUARDED_PREFIX):
            try:
                live_token = read_secret(store_path, TOKEN_SUFFIX)
            except CredentialError as exc:
                # Middleware runs outside the exception-handler layer, so the handler above
                # cannot catch this one. 503: a file the owner fixes.
                return JSONResponse(status_code=503, content={"detail": str(exc)})
            if live_token is None or not _token_matches(request, live_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "baraza needs the session token from the file beside your store."},
                )
        return await call_next(request)

    def opened(*, writable: bool = False) -> Store:
        """One connection per request, and the request says which kind it needs.

        A read opens SQLite's read-only mode, so it takes no write lock: a screen loads
        during an import, and a restored read-only backup opens at all.
        """
        return connect(store_path, read_only=not writable)

    def usable_thresholds(store: Store) -> tuple[Thresholds, str | None]:
        """The organizer's thresholds, or the defaults plus the reason theirs are not
        usable — never an exception.

        The store refuses to fall back silently, and is right to: a roster that re-labels
        itself because a setting was dropped is the worst of both. But an unreported
        refusal 500s every screen that needs thresholds, including the settings screen
        that could write a good set back — locking the organizer out of the fix.

        So the caller gets a usable set plus the sentence to show, and decides whether
        serving numbers under the defaults would be a lie (the data screens) or the only
        way out (settings).
        """
        try:
            return store.thresholds(), None
        except ValueError as exc:
            logger.warning("stored thresholds are unusable: %s", exc)
            return Thresholds(), str(exc)

    def require_thresholds(store: Store) -> Thresholds:
        """For the screens that must not answer under settings the organizer did not
        choose. A 409 with the reason and where to go, rather than a 500."""
        thresholds, problem = usable_thresholds(store)
        if problem is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{problem} Open Settings and save a set of thresholds to fix this.",
            )
        return thresholds

    @router.get("/status")
    def status() -> dict[str, Any]:
        """What the first-run screen branches on: is there anything in here yet."""
        store = opened()
        try:
            events, attendances = store.calendar()
            members = store.members()
            _, settings_problem = usable_thresholds(store)
        finally:
            store.close()
        return {
            "store_path": str(store_path),
            "events": len(events),
            # People the store HOLDS — everyone with a member row. NOT the overview's
            # count, which is people analytics could score. Named apart because one name
            # for both makes two screens disagree about the size of the same community.
            "people_in_store": len(members),
            "attendances": len(attendances),
            # The refusal, surfaced. Without this the app went on reporting itself
            # healthy while three screens 500'd.
            "settings_unusable": settings_problem,
            # Attendances, not events — "is there anything to show", not "is the store
            # non-empty". Naming a pending event writes an event row with no guests
            # attached yet; on `bool(events)` that flipped the first-run screen off and
            # dropped the organizer onto an overview reading "1 event, 0 people",
            # stranded from the events they had not named and from the instruction to
            # read the folder again. Found by walking it, not by a test.
            "has_data": bool(attendances),
            # Whether a key is on disk, never the key. The first-run screen needs to
            # know which of the two paths is already set up; nothing needs the value,
            # and an endpoint that hands a credential back is one that hands it to
            # whatever else eventually reaches this port.
            "luma_key_configured": read_secret(store_path, LUMA_KEY_SUFFIX) is not None,
        }

    @router.get("/thresholds")
    def get_thresholds() -> dict[str, Any]:
        """The organizer's boundaries — and **this one never refuses**.

        It is the screen that writes a good set back, so it is the way out of an
        unusable stored set. Failing here the way the data screens do would leave the
        organizer locked out of the fix by the bug. When the stored set will not
        validate, the defaults come back to render with and ``stored_is_unusable``
        carries the reason, so the form arrives populated and the screen can say what
        happened rather than showing plausible numbers as if they were theirs.
        """
        store = opened()
        try:
            thresholds, problem = usable_thresholds(store)
        finally:
            store.close()
        return vars(thresholds) | {"stored_is_unusable": problem}

    @router.put("/thresholds")
    def put_thresholds(body: ThresholdsBody) -> dict[str, Any]:
        """Set them. The cross-field rules live in :class:`Thresholds`, so a set that
        is individually valid but incoherent (a champion floor below the regular one)
        comes back as a 422 with that reason rather than a 500."""
        try:
            thresholds = Thresholds(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store = opened(writable=True)
        try:
            store.set_thresholds(thresholds)
        finally:
            store.close()
        return vars(thresholds)

    @router.post("/import/folder")
    def import_from_folder(body: ImportFolderBody) -> dict[str, Any]:
        """Read a folder of Luma guest-list exports. The organizer's normal loop —
        drop another export in and press this again.

        Events the resolver could not name come back under ``pending`` with a 200, not
        an error: their guests are read and waiting, and the CSVs stay in the folder,
        so nothing is lost and nothing has to be uploaded twice.
        """
        store = opened(writable=True)
        try:
            result = import_folder(store, body.path, now=clock(), resolve=resolve)
        except NotADirectoryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            store.close()
        return {
            "files_read": result.files_read,
            "files_skipped": list(result.files_skipped),
            "events": result.events,
            "members": result.members,
            "attendances": result.attendances,
            "pending": [vars(event) | {"sources": list(event.sources)} for event in result.pending],
            "warnings": list(result.warnings),
        }

    @router.get("/import/pending")
    def pending_events() -> dict[str, Any]:
        """Events whose guests were read but which still need a name and a date.

        Served from the store, never from an import pass's return value: a list that lives
        only in one response can be shown exactly once, so a reload, a navigation or
        arriving from the CLI finds no sign that work is waiting.

        ``folder`` is the last folder an import read, so the screen can offer it back —
        naming an event finishes only when that folder is read again.
        """
        store = opened()
        try:
            pending = store.pending_events()
            folder = store.last_import_folder()
        finally:
            store.close()
        return {"pending": pending, "folder": folder}

    @router.post("/import/luma")
    def import_from_luma(body: ImportLumaBody) -> dict[str, Any]:
        """Read the calendar straight from Luma. The paid path.

        The key is **probed before it is stored** — one cheap authenticated read — so a
        typo comes back as "that key was refused" rather than replacing the key that
        worked and leaving the organizer locked out of their own calendar with no way
        back but retyping it.
        """
        key = body.api_key or read_secret(store_path, LUMA_KEY_SUFFIX)
        if not key:
            raise HTTPException(status_code=400, detail="No Luma API key: paste one, or use the folder import.")

        client = luma_client(key)
        try:
            client.probe()
        except Exception as exc:  # the client raises RuntimeError, httpx raises its own
            logger.warning("Luma rejected the key or was unreachable: %s", exc)
            # Deliberately not echoed: the message carries Luma's own response text, and
            # this one is about to be rendered on the organizer's screen.
            raise HTTPException(status_code=502, detail="Luma refused that API key, or could not be reached.") from exc

        if body.api_key:
            write_secret(store_path, LUMA_KEY_SUFFIX, body.api_key)

        store = opened(writable=True)
        try:
            result = import_luma_api(store, client, now=clock())
        except (RuntimeError, OSError) as exc:
            # The probe above is carefully explained and everything after it was left
            # bare — so a Luma outage, a dropped connection or a rate
            # limit PART-WAY THROUGH the import surfaced as a 500 with a traceback. It is
            # the more likely failure of the two: the probe is one request and the import
            # is hundreds. Same shape of answer as the probe's, for the same reason.
            logger.warning("Luma import failed after the key was accepted: %s", exc)
            raise HTTPException(
                status_code=502,
                detail=(
                    "Luma stopped responding part-way through the import. "
                    "Nothing was left half-written — try again."
                ),
            ) from exc
        finally:
            store.close()
        return {
            "events": result.events,
            "members": result.members,
            "attendances": result.attendances,
            "warnings": list(result.warnings),
        }

    @router.put("/events/{event_id}")
    def put_event(event_id: str, body: EventBody) -> dict[str, Any]:
        """Name and date an event the resolver could not find.

        Writing the row is the whole mechanism: the next import over the same folder finds
        the event here first and attaches its guests, without another lookup and without the
        organizer's answer being overwritten by one.
        """
        event = StoredEvent(event_id, body.title, body.starts_at, body.ends_at)
        store = opened(writable=True)
        try:
            store.upsert_events([event])
            # Read back rather than echo. The body carries a title and dates only, and a
            # field the caller says nothing about is left alone rather than blanked, so
            # echoing the request would report empties the store does not hold.
            stored = next((e for e in store.events() if e.event_id == event_id), event)
        except ValueError as exc:  # a naive datetime — the store refuses it on write
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            store.close()
        return vars(stored) | {"tags": list(stored.tags)}

    @router.get("/events")
    def events() -> dict[str, Any]:
        """Every event, newest first, with what each one measured.

        Independent of the thresholds, unlike ``/overview``. Settings lists these, and
        Settings is the way back from a threshold set that will not validate, so this must
        render when they are broken.
        """
        store = opened()
        try:
            rows = store.events()
        finally:
            store.close()
        return {
            "events": [
                {
                    "event_id": event.event_id,
                    "title": event.title,
                    "starts_at": event.starts_at,
                    "attendance_evidence": event.attendance_evidence,
                }
                for event in sorted(rows, key=lambda e: (e.starts_at, e.event_id), reverse=True)
            ]
        }

    @router.put("/events/{event_id}/attendance-evidence")
    def put_attendance_evidence(event_id: str, body: AttendanceEvidenceBody) -> dict[str, Any]:
        """Declare whether an event recorded attendance. Never inferred: an event with no
        check-ins is either one nobody scanned or one nobody came to, and the rows are
        identical.
        """
        store = opened(writable=True)
        try:
            store.set_attendance_evidence(event_id, body.attendance_evidence)
            # Read back rather than echo, as `put_event` does. `next` without a default,
            # not an `assert`: `python -O` strips asserts and would leave `vars(None)`.
            stored = next(e for e in store.events() if e.event_id == event_id)
        except ValueError as exc:  # no such event — the store refuses the write
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        finally:
            store.close()
        return vars(stored) | {"tags": list(stored.tags)}

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        """The first screen: turnout per event, and the lifecycle mix beside it."""
        now = clock()
        store = opened()
        try:
            events, attendances = store.calendar()
            thresholds = require_thresholds(store)
        finally:
            store.close()
        members = analyze_members(events, attendances, now=now, thresholds=thresholds)
        turnout = event_attendance(events, attendances, now=now)
        return {
            "events": [vars(row) | {"show_rate": row.show_rate} for row in turnout],
            "lifecycle_mix": lifecycle_mix(members),
            # **People analytics could SCORE** — everyone with at least one attendance
            # record. See the note on `/status`'s `people_in_store`:
            # the two counts answer different questions and were both called "members".
            "people_scored": len(members),
        }

    @router.get("/people")
    def people() -> dict[str, Any]:
        """The roster, which doubles as the table view behind every chart."""
        now = clock()
        store = opened()
        try:
            events, attendances = store.calendar()
            thresholds = require_thresholds(store)
            known = {m.member_id: m for m in store.members()}
        finally:
            store.close()
        members = analyze_members(events, attendances, now=now, thresholds=thresholds)
        return {"people": [_member_row(m, known) for m in members]}

    @router.get("/people/{member_id}")
    def person(member_id: str) -> dict[str, Any]:
        """One member, and every event since they first appeared — the ones they
        ignored included, because that is what makes a no-show a pattern."""
        now = clock()
        store = opened()
        try:
            events, attendances = store.calendar()
            thresholds = require_thresholds(store)
            known = {m.member_id: m for m in store.members()}
        finally:
            store.close()

        members = {m.member_id: m for m in analyze_members(events, attendances, now=now, thresholds=thresholds)}
        if member_id not in members:
            raise HTTPException(status_code=404, detail=f"no member {member_id!r} in this store")
        return {
            "member": _member_row(members[member_id], known),
            "timeline": [vars(entry) for entry in member_timeline(member_id, events, attendances)],
        }

    @router.get("/retention")
    def retention() -> dict[str, Any]:
        """The two cohort views: the per-month grid, and per-event return beside it.

        The grid ships with what the UI needs to explain each kind of gap. A
        legend that cannot say which cannot explain its own blanks.

        There are three kinds and all three ship: ``months`` minus ``active_months``
        is "no event ran"; past the end of a row is "not yet"; and
        ``in_progress_month`` is "this month has not finished being counted". Sending
        only the first two would leave the UI describing the
        current month's blanks as "no event", which is a different and wrong story.
        """
        now = clock()
        store = opened()
        try:
            events, attendances = store.calendar()
        finally:
            store.close()
        grid = retention_grid(events, attendances, now=now)
        return {
            "months": list(grid.months),
            "active_months": sorted(grid.active_months),
            "in_progress_month": grid.in_progress_month,
            "cohorts": [vars(cohort) | {"attended": list(cohort.attended)} for cohort in grid.cohorts],
            # Per-EVENT cohorts, beside the per-month grid rather than replacing it: a grid
            # row is the first-timers of a month, a row here is the whole room at one event.
            "event_cohorts": [
                vars(cohort) | {"return_rate": cohort.return_rate}
                for cohort in event_cohorts(events, attendances, now=now)
            ],
        }

    app.include_router(router)
    # Last, and that ordering is load-bearing: the UI claims `/{path:path}`, so a
    # route registered after it would never be reached.
    mount_ui(app)
    return app


__all__ = [
    "ALLOWED_HOSTS",
    "GUARDED_PREFIX",
    "TOKEN_HEADER",
    "TOKEN_PARAM",
    "UNSAFE_METHODS",
    "ImportLumaBody",
    "EventBody",
    "ImportFolderBody",
    "ThresholdsBody",
    "create_app",
    "hostname_of",
]
