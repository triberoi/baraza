"""The API seam for the organizer's attendance declaration.

The arithmetic is tested in ``test_attendance_evidence.py`` and ``test_event_cohorts.py``
over a far wider input space than HTTP calls could reach. What is defended here is the
seam: that the declaration can be written and read back, that a bad one is refused with a
sentence rather than stored, that the screens are given what they need to SAY which
numbers they are showing, and that the screen holding the control can still render when
the thresholds beside it cannot.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baraza.store import StoredAttendance, StoredEvent, StoredMember, connect
from baraza.web.app import TOKEN_HEADER, create_app

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)
ORIGIN = {"origin": "http://127.0.0.1"}

EVENTS = [
    StoredEvent("e1", "January", D(2026, 1, 10)),
    StoredEvent("e2", "February", D(2026, 2, 10)),
]
MEMBERS = [StoredMember("mem_a", "amy@x.com", "Amy"), StoredMember("mem_b", "bo@x.com", "Bo")]
ATTENDANCES = [
    StoredAttendance("e1", "mem_a", "attended"),
    StoredAttendance("e1", "mem_b", "no_show"),
    # February took no attendance, so both of these are committed-but-unscanned.
    StoredAttendance("e2", "mem_a", "no_show"),
    StoredAttendance("e2", "mem_b", "no_show"),
]


def _client(app) -> TestClient:  # noqa: ANN001
    return TestClient(app, base_url="http://127.0.0.1", headers={**ORIGIN, TOKEN_HEADER: app.state.token})


@pytest.fixture
def loaded(tmp_path: Path) -> TestClient:
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events(EVENTS)
    store.upsert_members(MEMBERS)
    store.upsert_attendances(ATTENDANCES)
    store.close()
    return _client(create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None))


def _declare(client: TestClient, event_id: str, evidence: str):  # noqa: ANN202
    return client.put(f"/api/events/{event_id}/attendance-evidence", json={"attendance_evidence": evidence})


# --- writing the declaration -----------------------------------------------------
def test_declaring_an_event_unmeasured_reads_back(loaded: TestClient) -> None:
    assert _declare(loaded, "e2", "registration_only").status_code == 200
    listed = {e["event_id"]: e["attendance_evidence"] for e in loaded.get("/api/events").json()["events"]}
    assert listed == {"e1": "checked_in", "e2": "registration_only"}


@pytest.mark.parametrize("value", ["", "yes", "REGISTRATION_ONLY", "checked-in"])
def test_an_unrecognized_declaration_is_a_422_naming_the_field(loaded: TestClient, value: str) -> None:
    """A 422 rather than a 500 from deeper in, and nothing written. The organizer's own UI
    cannot send one of these, so the caller getting it is a script — which needs to be told
    what the field accepts, not handed a traceback."""
    response = _declare(loaded, "e1", value)
    assert response.status_code == 422
    assert "attendance_evidence" in str(response.json())
    listed = {e["event_id"]: e["attendance_evidence"] for e in loaded.get("/api/events").json()["events"]}
    assert listed["e1"] == "checked_in", "a refused write leaves the store alone"


def test_declaring_something_about_an_unknown_event_is_a_404(loaded: TestClient) -> None:
    """Not a silent success. The screen would go on showing a setting the store does not
    hold, until the next reload quietly disagreed with it."""
    assert _declare(loaded, "nope", "registration_only").status_code == 404


# --- what the screens are given --------------------------------------------------
def test_the_overview_withholds_the_rate_and_names_the_evidence(loaded: TestClient) -> None:
    """Both halves in one response, because a screen needs both to do its job: the rate is
    absent so it cannot be drawn, and the evidence is present so the row can say why."""
    _declare(loaded, "e2", "registration_only")
    rows = {e["event_id"]: e for e in loaded.get("/api/overview").json()["events"]}

    assert rows["e2"]["show_rate"] is None
    assert rows["e2"]["attendance_evidence"] == "registration_only"
    assert rows["e2"]["attended"] == 2, "both said yes, and nobody was at a door"
    assert rows["e2"]["no_shows"] == 0
    # The measured event is untouched: one of two turned up.
    assert rows["e1"]["show_rate"] == pytest.approx(0.5)


def test_a_roster_row_can_say_how_many_of_its_attendances_were_unmeasured(loaded: TestClient) -> None:
    """The split the People screen renders. Without it the roster shows a total and no way
    to tell what it is made of, which is the failure the whole surfacing rule exists for."""
    _declare(loaded, "e2", "registration_only")
    people = {p["member_id"]: p for p in loaded.get("/api/people").json()["people"]}

    assert (people["mem_a"]["events_attended"], people["mem_a"]["unmeasured_attendances"]) == (2, 1)
    assert (people["mem_b"]["events_attended"], people["mem_b"]["unmeasured_attendances"]) == (1, 1)


def test_a_persons_timeline_marks_the_unmeasured_event(loaded: TestClient) -> None:
    """So the person page cannot contradict the roster row that led someone to it."""
    _declare(loaded, "e2", "registration_only")
    timeline = {t["event_id"]: t for t in loaded.get("/api/people/mem_b").json()["timeline"]}

    assert timeline["e2"]["status"] == "attended"
    assert timeline["e2"]["attendance_evidence"] == "registration_only"


def test_the_retention_payload_carries_per_event_cohorts(loaded: TestClient) -> None:
    """The newest event's blank has to survive serialization as null. Collapsed to 0 it
    reads as a room that never came back, on every fresh import."""
    cohorts = loaded.get("/api/retention").json()["event_cohorts"]
    assert [c["event_id"] for c in cohorts] == ["e1", "e2"]
    assert cohorts[-1]["returned"] is None
    assert cohorts[-1]["return_rate"] is None


# --- the listing that Settings renders -------------------------------------------
def test_the_event_list_is_newest_first(loaded: TestClient) -> None:
    """The event somebody is most likely to be declaring something about is the one they
    just ran."""
    assert [e["event_id"] for e in loaded.get("/api/events").json()["events"]] == ["e2", "e1"]


def test_the_event_list_answers_over_an_empty_store(tmp_path: Path) -> None:
    connect(tmp_path / "b.db", create=True).close()
    client = _client(create_app(tmp_path / "b.db", clock=lambda: NOW, resolve=None))
    assert client.get("/api/events").json() == {"events": []}


def test_the_event_list_still_answers_when_the_thresholds_do_not_validate(tmp_path: Path) -> None:
    """**Why this endpoint exists rather than reusing `/overview`.**

    Settings is the way back from a stored threshold set that will not validate, and this
    list lives on Settings. Sourcing it from `/overview` — which needs working thresholds
    — would have made the screen unreachable in exactly the state it exists to repair,
    which is the shape of the defect `/thresholds` was already fixed for.
    """
    path = tmp_path / "b.db"
    db = connect(path, create=True)
    db.upsert_events(EVENTS)
    db._db.execute(  # noqa: SLF001
        "INSERT INTO settings (key, value) VALUES ('thresholds', ?)",
        ['{"regular_min_events": 1, "champion_min_events": 1, "champion_min_rate": 0.5, "lapsed_after_days": 90}'],
    )
    db._db.commit()  # noqa: SLF001
    db.close()
    client = _client(create_app(path, clock=lambda: NOW, resolve=None))

    assert client.get("/api/overview").status_code != 200, "the fixture must really be broken"
    assert client.get("/api/events").status_code == 200
    assert _declare(client, "e2", "registration_only").status_code == 200
