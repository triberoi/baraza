"""Tests for the JSON API (``baraza.web.app``).

The API holds no arithmetic, so these do not re-test the numbers — `test_scoring.py`,
`test_views.py` and `test_retention.py` already do, over a far wider input space than a
handful of HTTP calls could. What is defended here is the seam:

- every endpoint answers over an **empty** store, because the first-run screen calls
  them before there is any data and a 500 there is the first thing a new user sees;
- the values the analytics layer worked to distinguish — a ``None`` score, a ``None``
  retention cell — survive serialization as ``null`` rather than collapsing to ``0``;
- a threshold change is visible in the very next response, which is the whole reason
  the store holds facts and not conclusions.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baraza.credentials import LUMA_KEY_SUFFIX, TOKEN_SUFFIX, read_secret, write_secret
from baraza.store import StoredAttendance, StoredEvent, StoredMember, connect
from baraza.web.app import TOKEN_HEADER, create_app

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

#: What the tool's own UI sends: an Origin (a browser attaches one to every non-GET
#: request, same-origin included) and the session token. A client that omits either is
#: not modelling the real thing.
ORIGIN = {"origin": "http://127.0.0.1"}


def _client(app) -> TestClient:  # noqa: ANN001
    return TestClient(app, base_url="http://127.0.0.1", headers={**ORIGIN, TOKEN_HEADER: app.state.token})

EVENTS = [
    StoredEvent("e1", "January", D(2026, 1, 10)),
    StoredEvent("e2", "February", D(2026, 2, 10)),
    StoredEvent("e3", "March", D(2026, 3, 10)),
    StoredEvent("e9", "Next month", D(2026, 7, 10)),
]
MEMBERS = [
    StoredMember("mem_a", "amy@x.com", "Amy"),
    StoredMember("mem_b", "bo@x.com", "Bo"),
    StoredMember("mem_c", "cy@x.com", "Cy"),
]
ATTENDANCES = [
    StoredAttendance("e1", "mem_a", "attended"),
    StoredAttendance("e2", "mem_a", "attended"),
    StoredAttendance("e3", "mem_a", "attended"),
    StoredAttendance("e2", "mem_b", "no_show"),
    StoredAttendance("e9", "mem_c", "registered"),
]


@pytest.fixture
def empty(tmp_path: Path) -> TestClient:
    connect(tmp_path / "b.db", create=True).close()
    return _client(create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None))


@pytest.fixture
def loaded(tmp_path: Path) -> TestClient:
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    store.upsert_attendances(ATTENDANCES)
    store.close()
    return _client(create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None))


ENDPOINTS = ["/api/status", "/api/thresholds", "/api/overview", "/api/people", "/api/retention"]


# --- the first-run case ----------------------------------------------------------
@pytest.mark.parametrize("path", ENDPOINTS)
def test_every_endpoint_answers_over_an_empty_store(empty: TestClient, path: str) -> None:
    """The first-run screen calls these before there is any data. A 500 here is the
    first thing a brand-new user would ever see from the tool."""
    assert empty.get(path).status_code == 200


def test_status_says_there_is_nothing_yet(empty: TestClient) -> None:
    body = empty.get("/api/status").json()
    assert body["has_data"] is False
    # `people_in_store`, not `members`: the overview counts a different population under
    # what used to be the same name.
    assert (body["events"], body["people_in_store"], body["attendances"]) == (0, 0, 0)
    assert body["settings_unusable"] is None


def test_an_event_with_no_guests_is_not_yet_something_to_show(tmp_path: Path) -> None:
    """``has_data`` means "is there anything to show", not "is the store non-empty".

    Naming a pending event writes an event row with no guests attached until the folder
    is read again. On ``bool(events)`` that flipped the first-run screen off and dropped
    the organizer onto an overview reading "1 event, 0 people" — away from the events
    they had not named yet and from the instruction telling them what to do next.
    Found by walking the screen, which is why the test is written from that direction.
    """
    path = tmp_path / "b.db"
    store = connect(path, create=True)
    store.upsert_events([StoredEvent("e1", "January", D(2026, 1, 10))])
    store.close()
    client = _client(create_app(path, clock=lambda: NOW, resolve=None))

    body = client.get("/api/status").json()
    assert body["events"] == 1
    assert body["has_data"] is False, "an event nobody attended is not yet a calendar to show"

    store = connect(path, create=True)
    store.upsert_members([StoredMember("mem_a", "amy@x.com", "Amy")])
    store.upsert_attendances([StoredAttendance("e1", "mem_a", "attended")])
    store.close()
    assert client.get("/api/status").json()["has_data"] is True


def test_an_empty_store_still_returns_the_whole_lifecycle_ramp(empty: TestClient) -> None:
    """Zero-filled, so the overview's bar has its axis on the very first render rather
    than growing categories as members arrive."""
    mix = empty.get("/api/overview").json()["lifecycle_mix"]
    assert list(mix) == ["prospect", "first_timer", "regular", "champion", "lapsed"]
    assert set(mix.values()) == {0}


# --- what must survive serialization ------------------------------------------------
def test_a_missing_score_serializes_as_null_not_zero(loaded: TestClient) -> None:
    """The distinction analytics went out of its way to make. JSON has a word for "no
    data" and it is not 0 — a UI that received 0 would draw an empty bar for someone
    whose only event has not happened yet."""
    people = {p["member_id"]: p for p in loaded.get("/api/people").json()["people"]}
    assert people["mem_c"]["score"] is None, "a member with only a future event has no score yet"
    assert people["mem_b"]["score"] == 0, "a member who had chances and took none genuinely scores zero"


def test_a_retention_cell_with_no_event_serializes_as_null(loaded: TestClient) -> None:
    """Zero in a retention grid means "they all left". April and May had no events."""
    grid = loaded.get("/api/retention").json()
    assert set(grid["months"]) - set(grid["active_months"]) == {"2026-04", "2026-05", "2026-06"}
    (cohort,) = grid["cohorts"]
    assert cohort["attended"][:3] == [1, 1, 1]
    assert cohort["attended"][3:] == [None, None, None]


def test_the_grid_ships_what_a_legend_needs_to_explain_its_blanks(loaded: TestClient) -> None:
    """All THREE reasons a cell is blank ship, not two. Sending only
    "no event" and "beyond the grid" left the UI describing the current month's blanks
    as "no event happened", which is a different and wrong story — and the UI's fallback
    did exactly that, because for a while those were the only nulls there were."""
    grid = loaded.get("/api/retention").json()
    assert grid["active_months"] == sorted(grid["active_months"]), "sorted, so the UI need not"
    assert set(grid["active_months"]) <= set(grid["months"])
    assert "in_progress_month" in grid
    if grid["in_progress_month"] is not None:
        assert grid["in_progress_month"] in grid["months"]


def test_future_events_are_absent_from_the_turnout_rows(loaded: TestClient) -> None:
    """They have no turnout to split; drawing them flat at zero makes a healthy
    calendar end in a cliff."""
    rows = loaded.get("/api/overview").json()["events"]
    assert [r["event_id"] for r in rows] == ["e1", "e2", "e3"]
    assert all("show_rate" in r for r in rows)


# --- the roster join ------------------------------------------------------------------
def test_a_roster_row_carries_both_halves(loaded: TestClient) -> None:
    """Identity from the store, judgment from analytics, joined on member_id — which is
    what lets ``Store.calendar()`` withhold the email from scoring at no cost to the UI."""
    people = {p["member_id"]: p for p in loaded.get("/api/people").json()["people"]}
    assert people["mem_a"]["name"] == "Amy" and people["mem_a"]["email"] == "amy@x.com"
    assert people["mem_a"]["lifecycle"] == "regular"
    assert people["mem_a"]["events_attended"] == 3


def test_a_member_with_no_stored_identity_still_appears(tmp_path: Path) -> None:
    """This module's writes cannot produce an orphan — foreign keys are on — but the
    file is the organizer's, and a SQLite browser or an older baraza can. Dropping the
    row would lose an attendance from every total; a null name loses only the name.

    Built by inserting behind the store's back with foreign keys off, which is exactly
    what a hand-edited file looks like on the way back in.
    """
    import sqlite3

    path = tmp_path / "b.db"
    store = connect(path, create=True)
    store.upsert_events(EVENTS)
    store.close()
    with sqlite3.connect(path) as raw:  # no `PRAGMA foreign_keys = ON` here, deliberately
        raw.execute("INSERT INTO attendances (event_id, member_id, status) VALUES ('e1', 'mem_ghost', 'attended')")

    client = _client(create_app(path, clock=lambda: NOW, resolve=None))
    (row,) = client.get("/api/people").json()["people"]
    assert row["member_id"] == "mem_ghost"
    assert row["name"] is None and row["email"] is None
    assert row["events_attended"] == 1
    assert client.get("/api/overview").json()["events"][0]["attended"] == 1


def test_one_person_returns_their_whole_era(loaded: TestClient) -> None:
    """Including the events they ignored — that is the view's entire point."""
    body = loaded.get("/api/people/mem_b").json()
    assert body["member"]["name"] == "Bo"
    assert [(e["event_id"], e["status"]) for e in body["timeline"]] == [
        ("e2", "no_show"),
        ("e3", None),
        ("e9", None),
    ]


def test_an_unknown_member_is_a_404_not_an_empty_page(loaded: TestClient) -> None:
    assert loaded.get("/api/people/mem_nope").status_code == 404


# --- thresholds ------------------------------------------------------------------------
def test_a_threshold_change_is_visible_in_the_next_response(loaded: TestClient) -> None:
    """The reason nothing is cached and the store holds no labels. Amy attended 3 of 3;
    raising the champion floor above 3 must demote her on the very next read."""
    assert loaded.get("/api/people").json()["people"][0]["lifecycle"] == "regular"

    put = loaded.put(
        "/api/thresholds",
        json={
            "regular_min_events": 2,
            "champion_min_events": 3,
            "champion_min_rate": 0.5,
            "lapsed_after_days": 90,
        },
    )
    assert put.status_code == 200
    assert loaded.get("/api/people").json()["people"][0]["lifecycle"] == "champion"
    assert loaded.get("/api/thresholds").json()["champion_min_events"] == 3


def test_thresholds_default_before_anyone_sets_them(empty: TestClient) -> None:
    assert empty.get("/api/thresholds").json() == {
        "regular_min_events": 2,
        "champion_min_events": 4,
        "champion_min_rate": 0.5,
        "lapsed_after_days": 90,
        # None when the stored set is fine — which is also how the screen tells "these
        # are your settings" from "these are the defaults, because yours are broken".
        "stored_is_unusable": None,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"regular_min_events": 1, "champion_min_events": 4, "champion_min_rate": 0.5, "lapsed_after_days": 90},
        {"regular_min_events": 2, "champion_min_events": 4, "champion_min_rate": 0.0, "lapsed_after_days": 90},
        {"regular_min_events": 2, "champion_min_events": 4, "champion_min_rate": 0.5, "lapsed_after_days": 0},
        {"regular_min_events": 5, "champion_min_events": 3, "champion_min_rate": 0.5, "lapsed_after_days": 90},
    ],
    ids=["regular-too-low", "rate-zero", "no-lapse-window", "champion-below-regular"],
)
def test_an_unusable_threshold_set_is_a_422(loaded: TestClient, body: dict[str, object]) -> None:
    """Including the last one, which is the interesting case: every field is
    individually fine and the set is still incoherent. That rule lives in
    ``Thresholds`` and must not have been lost on the way through the API."""
    assert loaded.put("/api/thresholds", json=body).status_code == 422


# --- import and manual entry ------------------------------------------------------------
def test_importing_a_folder_that_is_not_there_is_a_400(empty: TestClient) -> None:
    """The organizer typed a path. A 400 naming it is an answer; a 500 is a bug report
    they cannot act on."""
    response = empty.post("/api/import/folder", json={"path": "/definitely/not/here"})
    assert response.status_code == 400
    assert "not a folder" in response.json()["detail"]


def test_an_unresolvable_event_comes_back_as_pending_with_a_200(empty: TestClient, tmp_path: Path) -> None:
    """Resolution failure falls back to manual entry, never an error. The
    endpoint runs with no network — the resolver would be a live call to an
    undocumented endpoint, and there is none here to reach it."""
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Book_Club_-_Guests_-_2026-01-10-00-00-00.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,,https://lu.ma/check-in/evt-zzz\n",
        encoding="utf-8",
    )
    body = empty.post("/api/import/folder", json={"path": str(folder)}).json()

    assert body["files_read"] == 1 and body["events"] == 0
    (pending,) = body["pending"]
    assert pending["luma_event_id"] == "evt-zzz"
    assert pending["inferred_title"] == "Book Club"
    assert pending["guests"] == 1
    assert empty.get("/api/status").json()["has_data"] is False


def test_naming_a_pending_event_writes_the_row_the_next_import_reads(empty: TestClient) -> None:
    """The manual-entry mechanism: the organizer's answer *is* the event row, which is
    what the next pass over the folder looks up before it asks Luma anything."""
    response = empty.put(
        "/api/events/evt-zzz",
        json={"title": "Book Club", "starts_at": "2026-03-01T18:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Book Club"
    assert empty.get("/api/status").json()["events"] == 1


def test_the_pending_list_is_served_from_the_store_not_the_last_response(
    empty: TestClient, tmp_path: Path
) -> None:
    """The defect this endpoint exists for: the pending list used to live only in one
    ``POST /import/folder`` response, so the Import screen could show it exactly once.
    A reload, or an import run in the terminal, found a screen with nothing on it.
    ``GET /import/pending`` answers from the store — the same list, any time — and
    carries the folder back so the screen can offer to read it again."""
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Book_Club_-_Guests_-_2026-01-10-00-00-00.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,,https://lu.ma/check-in/evt-zzz\n",
        encoding="utf-8",
    )
    empty.post("/api/import/folder", json={"path": str(folder)})

    body = empty.get("/api/import/pending").json()
    (pending,) = body["pending"]
    assert pending["luma_event_id"] == "evt-zzz"
    assert pending["inferred_title"] == "Book Club"
    assert body["folder"] == str(folder.resolve())

    # Naming the event retires the entry with no further import.
    empty.put("/api/events/evt-zzz", json={"title": "Book Club", "starts_at": "2026-03-01T18:00:00Z"})
    assert empty.get("/api/import/pending").json()["pending"] == []


def test_an_untouched_store_has_an_empty_pending_list(empty: TestClient) -> None:
    assert empty.get("/api/import/pending").json() == {"pending": [], "folder": None}


# --- the paid path ------------------------------------------------------------------------
def test_importing_from_luma_with_no_key_anywhere_is_a_400(empty: TestClient) -> None:
    """Not a 500 and not a 401 — the organizer simply has not set this path up, and the
    message names the other one they can use instead."""
    response = empty.post("/api/import/luma", json={})
    assert response.status_code == 400
    assert "folder import" in response.json()["detail"]


def test_a_key_luma_refuses_is_never_written_to_disk(tmp_path: Path) -> None:
    """The reason the key is probed before it is stored. Storing first would mean a typo
    replaces the key that worked and locks the organizer out of their own calendar with
    no way back but retyping it."""
    connect(tmp_path / "b.db", create=True).close()
    write_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX, "the-good-key")

    def refuses(_key: str) -> object:
        class Rejecting:
            def probe(self) -> None:
                raise RuntimeError("Luma API 401 on /calendar/list-events")

        return Rejecting()

    app = create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None, luma_client=refuses)
    client = _client(app)
    response = client.post("/api/import/luma", json={"api_key": "typo"})

    assert response.status_code == 502
    assert read_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX) == "the-good-key"


def test_a_good_key_is_stored_once_and_the_calendar_lands(tmp_path: Path) -> None:
    """The whole paid path through the endpoint: paste a key, get the calendar, and the
    second import needs no key because the first one kept it."""
    from baraza.ingest import LumaEvent, LumaGuest

    calls: list[str | None] = []

    def factory(key: str) -> object:
        calls.append(key)

        class Calendar:
            def probe(self) -> None: ...

            def list_events(self) -> list[LumaEvent]:
                return [LumaEvent("evt-1", "January", D(2026, 1, 10), D(2026, 1, 10, 20), 50, "Hall", None, ("py",))]

            def list_event_guests(self, event_api_id: str) -> list[LumaGuest]:
                return [LumaGuest("g1", "approved", "amy@x.com", "Amy", D(2026, 1, 1), D(2026, 1, 10, 18))]

        return Calendar()

    connect(tmp_path / "b.db", create=True).close()
    client = _client(create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None, luma_client=factory))

    first = client.post("/api/import/luma", json={"api_key": "sk-good"})
    assert first.status_code == 200
    assert (first.json()["events"], first.json()["members"]) == (1, 1)
    assert read_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX) == "sk-good"

    second = client.post("/api/import/luma", json={})
    assert second.status_code == 200
    assert calls == ["sk-good", "sk-good"], "the second call reused the stored key"
    assert client.get("/api/people").json()["people"][0]["name"] == "Amy"


def test_lumas_own_words_are_not_echoed_to_the_screen(tmp_path: Path) -> None:
    """The client's exception message carries Luma's response text, which is
    platform-controlled. It goes to the log; what reaches the organizer is ours."""
    connect(tmp_path / "b.db", create=True).close()

    def refuses(_key: str) -> object:
        class Rejecting:
            def probe(self) -> None:
                raise RuntimeError("SOMETHING LUMA SAID")

        return Rejecting()

    app = create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None, luma_client=refuses)
    detail = _client(app).post("/api/import/luma", json={"api_key": "k"}).json()["detail"]
    assert "SOMETHING LUMA SAID" not in detail


def test_status_says_whether_a_key_is_set_up_without_handing_it_over(tmp_path: Path) -> None:
    """The first-run screen needs to know which path is configured. Nothing needs the
    value, and an endpoint that returns a credential is one that returns it to whatever
    else eventually reaches this port."""
    connect(tmp_path / "b.db", create=True).close()
    app = create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None)
    client = _client(app)
    assert client.get("/api/status").json()["luma_key_configured"] is False

    write_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX, "sk-live-something")
    body = client.get("/api/status").json()
    assert body["luma_key_configured"] is True
    assert "sk-live-something" not in str(body)


def test_naming_an_event_without_a_timezone_is_a_422(empty: TestClient) -> None:
    """A naive datetime reads back as a different instant. The store refuses it on
    write; this is that refusal reaching the organizer as a 422 rather than a 500."""
    response = empty.put("/api/events/evt-zzz", json={"title": "Book Club", "starts_at": "2026-03-01T18:00:00"})
    assert response.status_code == 422


def test_naming_an_event_reports_what_the_store_holds_not_what_was_sent(
    loaded: TestClient, tmp_path: Path
) -> None:
    """The consequence here. The body carries a title and dates and
    nothing else, and a field a caller says nothing about is now LEFT ALONE rather than
    blanked — so an event that already has a venue and tags keeps them. Echoing the
    request back would report empties the store does not hold, which is the same class
    of untruth as the wipe itself: a response that disagrees with the file."""
    # Seeded here rather than taken from EVENTS: the fixture has no event carrying both
    # a venue and tags, and those two fields are the whole subject.
    before = StoredEvent("evt-rich", "From the API", NOW, None, "The Hall", ("python", "beginners"))
    seed = connect(tmp_path / "b.db")
    seed.upsert_events([before])
    seed.close()

    response = loaded.put(
        f"/api/events/{before.event_id}",
        json={"title": "Renamed by hand", "starts_at": before.starts_at.isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Renamed by hand"
    assert body["venue"] == before.venue
    assert body["tags"] == list(before.tags)

    # ...and the response is the file, not a hopeful copy of it.
    store = connect(tmp_path / "b.db")
    stored = next(e for e in store.events() if e.event_id == before.event_id)
    store.close()
    assert (stored.title, stored.venue, stored.tags) == ("Renamed by hand", before.venue, before.tags)


def test_a_rejected_threshold_set_does_not_reach_the_file(loaded: TestClient) -> None:
    """A 422 that had already written would leave the roster re-labelled by a set the
    organizer was just told was invalid."""
    before = loaded.get("/api/thresholds").json()
    loaded.put(
        "/api/thresholds",
        json={
            "regular_min_events": 5,
            "champion_min_events": 3,
            "champion_min_rate": 0.5,
            "lapsed_after_days": 90,
        },
    )
    assert loaded.get("/api/thresholds").json() == before


def test_a_host_header_in_capitals_is_the_same_host(loaded: TestClient) -> None:
    """Hostnames are case-insensitive by definition, and the ``Origin`` half
    of this guard already knew that — ``urlsplit`` lowercases for you. The ``Host`` half
    compared the raw header against a lowercase set, so ``LOCALHOST`` was refused by one
    half of a guard the other half would have admitted. A check that is stricter by
    accident still fails a real user, and nobody debugging it looks at capitalization."""
    for host in ("LOCALHOST", "LocalHost:8000", "127.0.0.1"):
        assert loaded.get("/api/status", headers={"host": host}).status_code == 200, host
    # ...and the guard still refuses what it is for: a request arriving under the
    # attacker's own name, which is what DNS rebinding needs.
    assert loaded.get("/api/status", headers={"host": "evil.example"}).status_code == 421


def test_a_non_ascii_token_is_refused_rather_than_a_500(
    loaded: TestClient, tmp_path: Path
) -> None:
    """``secrets.compare_digest`` on ``str`` requires both sides to be ASCII
    and raises ``TypeError`` otherwise, so a presented token with an accented character
    turned every request into a 500 with nothing in it to explain why — including the
    request for the page that would have said so. Not matching is the correct answer to
    a wrong token."""
    # Over the wire the header itself cannot carry one — httpx refuses to encode it,
    # which is HTTP's rule and not ours — so the two reachable vectors are the query
    # parameter and a token FILE someone edited. Both used to be a 500.
    assert loaded.get("/api/status?token=café", headers={TOKEN_HEADER: ""}).status_code == 401

    path = tmp_path / "b.db"
    connect(path, create=True).close()
    write_secret(path, TOKEN_SUFFIX, "café")
    client = TestClient(create_app(path, clock=lambda: NOW, resolve=None), base_url="http://127.0.0.1")
    assert client.get("/api/status", headers={TOKEN_HEADER: "anything"}).status_code == 401


def test_an_empty_folder_box_is_a_422_and_not_a_successful_import_of_nothing(
    loaded: TestClient,
) -> None:
    """``Path("")`` is ``.``, so submitting the form untouched scanned
    whatever directory the server was started from, found no Luma exports, and reported
    a successful import of nothing — leaving the organizer looking at an empty app that
    had just told them it worked."""
    for path in ("", "   ", "\t"):
        response = loaded.post("/api/import/folder", json={"path": path})
        assert response.status_code == 422, repr(path)


def test_an_unusable_settings_row_does_not_lock_the_organizer_out_of_the_fix(
    tmp_path: Path,
) -> None:
    """The whole shape of this seam in one defect.

    The store deliberately REFUSES to fall back to defaults when a stored set no longer
    validates, and it is right to — a roster that quietly re-labels itself because a
    setting was dropped is the worst of both. But nothing turned that refusal into an
    answer: the three data screens died with a 500 and a traceback, ``/status`` did not
    read thresholds at all and so went on reporting the app healthy, and the one
    endpoint that could write a good set back was itself one of the broken three. The
    organizer was locked out of the fix by the bug.
    """
    path = tmp_path / "b.db"
    db = connect(path, create=True)
    # Written straight past `set_thresholds`, which is the only way this happens: an
    # older baraza whose rules were looser, or a hand edit in a SQLite browser.
    db._db.execute(  # noqa: SLF001
        "INSERT INTO settings (key, value) VALUES ('thresholds', ?)",
        ['{"regular_min_events": 1, "champion_min_events": 1, "champion_min_rate": 0.5, "lapsed_after_days": 90}'],
    )
    db._db.commit()  # noqa: SLF001
    db.close()
    client = _client(create_app(path, clock=lambda: NOW, resolve=None))

    # The way out is open, and says which numbers these are.
    settings = client.get("/api/thresholds")
    assert settings.status_code == 200
    assert settings.json()["stored_is_unusable"], "the screen must be able to say these are not theirs"

    # The app stops calling itself healthy.
    assert client.get("/api/status").json()["settings_unusable"]

    # The three screens that need thresholds refuse, with the reason and where to go —
    # not a traceback. (Retention is NOT one of them: it takes no thresholds, which is
    # why the finding says three screens and not four.)
    for endpoint in ("/api/overview", "/api/people", "/api/people/mem_a"):
        response = client.get(endpoint)
        assert response.status_code == 409, endpoint
        assert "Settings" in response.json()["detail"], endpoint

    # And writing a good set puts everything back.
    assert (
        client.put(
            "/api/thresholds",
            json={
                "regular_min_events": 2,
                "champion_min_events": 4,
                "champion_min_rate": 0.5,
                "lapsed_after_days": 90,
            },
        ).status_code
        == 200
    )
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/status").json()["settings_unusable"] is None


def test_a_luma_outage_part_way_through_an_import_is_a_sentence(tmp_path: Path) -> None:
    """The probe is carefully explained and everything after it was left
    bare, so an outage during the import — the more likely of the two, since the probe
    is one request and the import is hundreds — surfaced as a 500 with a traceback."""

    class DiesPartWay:
        def probe(self) -> None: ...
        def list_events(self) -> list[object]:
            raise RuntimeError("Luma API 503 on /calendar/list-events")

    path = tmp_path / "b.db"
    connect(path, create=True).close()
    client = _client(create_app(path, clock=lambda: NOW, resolve=None, luma_client=lambda _key: DiesPartWay()))

    response = client.post("/api/import/luma", json={"api_key": "a-good-key"})
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "try again" in detail
    assert "503" not in detail, "Luma's own response text must not reach the organizer's screen"


def test_a_store_that_vanishes_under_a_running_server_answers_with_its_own_sentence(tmp_path: Path) -> None:
    """The store is a file the organizer copies, backs up and syncs, so it
    moving while the app is open is ordinary rather than exotic.

    Every data route calls `opened()` bare, and nothing caught `StoreError` — so the
    message the exception already carries was discarded and the browser got a 500 with a
    `text/plain` body it could not read a `detail` out of. Every screen then said "The
    request failed (500)." with no hint that the file had moved.
    """
    store_path = tmp_path / "gone.db"
    connect(store_path, create=True).close()
    app = create_app(store_path)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {TOKEN_HEADER: app.state.token, "host": "127.0.0.1"}

    store_path.rename(tmp_path / "moved-away.db")

    response = client.get("/api/overview", headers=headers)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "gone.db" in detail and "baraza import" in detail
