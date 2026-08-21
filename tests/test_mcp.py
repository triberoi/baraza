"""The MCP surface.

:mod:`baraza.mcp.tools` imports no protocol, so everything an MCP client can actually get
is tested everywhere, including where the optional extra is absent. Only the registration
shim is gated on it.

KEEP THE GATE ASKING ABOUT THE DISTRIBUTION, never about the module the shim imports.
``importorskip("mcp.server.fastmcp")`` makes a rename indistinguishable from an absent
extra: when that module was renamed away, these skipped with "the optional extra is not
installed" in an environment where it was installed, and the job went green having
exercised nothing.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baraza.mcp import tools
from baraza.store import StoredAttendance, StoredEvent, StoredMember, connect

#: Whether the optional extra is installed — a question about the DISTRIBUTION, never about
#: the module the shim imports. See the module docstring for why that distinction is the
#: whole fix.
_EXTRA_INSTALLED = importlib.util.find_spec("mcp") is not None

needs_the_extra = pytest.mark.skipif(
    not _EXTRA_INSTALLED,
    reason="the optional `mcp` extra is not installed (pyproject `[project.optional-dependencies] mcp`)",
)

# The `broken_until_the_shim_is_fixed` xfail(strict) marker that used to sit here is GONE,
# It declared the known failures rather than concealing them, and strict meant that the
# moment the shim was fixed they would XPASS and redden CI until
# somebody came back and removed it. That is exactly what happened. The two tests below now
# run for real, in the one environment where the extra installs.

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)

EVENTS = [
    StoredEvent("e1", "January", D(2026, 1, 10)),
    StoredEvent("e2", "February", D(2026, 2, 10)),
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
    StoredAttendance("e1", "mem_b", "no_show"),
    StoredAttendance("e9", "mem_c", "registered"),
]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    db = connect(path, create=True)
    db.upsert_events(EVENTS)
    db.upsert_members(MEMBERS)
    db.upsert_attendances(ATTENDANCES)
    db.close()
    return path


@pytest.fixture
def empty(tmp_path: Path) -> Path:
    path = tmp_path / "empty.db"
    connect(path, create=True).close()
    return path


# --- what a model is handed ------------------------------------------------------
def test_the_overview_is_the_same_numbers_the_screen_shows(store: Path) -> None:
    """Same functions underneath, so a model and an organizer cannot end up describing
    different communities."""
    body = tools.overview(store, now=NOW)
    assert (body["events_held"], body["people"], body["attendances"]) == (2, 3, 2)
    assert body["show_rate"] == round(2 / 3, 3)
    assert body["lifecycle_mix"]["regular"] == 1


def test_a_missing_rate_is_null_not_zero(empty: Path) -> None:
    """The distinction the whole stack preserves. A model handed 0 will report a 0%
    turnout for a community that has simply not run an event yet."""
    assert tools.overview(empty, now=NOW)["show_rate"] is None


def test_every_tool_answers_over_an_empty_store(empty: Path) -> None:
    """A model will ask before there is data, because it cannot see that there is none."""
    assert tools.overview(empty, now=NOW)["people"] == 0
    assert tools.people(empty, now=NOW)["people"] == []
    assert tools.retention(empty, now=NOW)["cohorts"] == []


def test_people_come_back_ranked(store: Path) -> None:
    body = tools.people(store, now=NOW)
    scores = [p["score"] for p in body["people"] if p["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    assert body["people"][0]["name"] == "Amy"


def test_the_mcp_surface_never_sends_an_email_derived_name(tmp_path: Path) -> None:
    """The README promises the assistant is sent names, never email addresses. A
    nameless member's on-screen stand-in is the email local part (``StoredMember.display_name``),
    so the MCP tools must return the real name or ``null`` — never that stand-in. Every other
    fixture has a name, which is why this drift shipped unnoticed."""
    path = tmp_path / "nameless.db"
    db = connect(path, create=True)
    db.upsert_events([StoredEvent("e1", "January", D(2026, 1, 10))])
    db.upsert_members([StoredMember("mem_x", "jane.doe@acme.com", "")])  # no name
    db.upsert_attendances([StoredAttendance("e1", "mem_x", "attended")])
    db.close()

    listed = tools.people(path, now=NOW)["people"]
    entry = next(p for p in listed if p["member_id"] == "mem_x")
    assert entry["name"] is None
    assert tools.person(path, "mem_x", now=NOW)["name"] is None
    # The email local part must appear nowhere in what the assistant is handed.
    assert "jane.doe" not in str(tools.people(path, now=NOW))
    assert "jane.doe" not in str(tools.person(path, "mem_x", now=NOW))


def test_a_truncated_roster_says_so(store: Path) -> None:
    """Silent truncation is the failure mode that matters here: a model handed half a
    community with no marker will describe it to the organizer as all of it."""
    body = tools.people(store, limit=1, now=NOW)
    assert (body["total"], body["returned"], body["truncated"]) == (3, 1, True)
    assert tools.people(store, limit=50, now=NOW)["truncated"] is False


def test_the_roster_is_capped_however_much_is_asked_for(store: Path) -> None:
    """A model asking for a million rows gets the ceiling, not a reply too large to
    return — and the cap is reported the same way any other truncation is."""
    assert tools.people(store, limit=10**6, now=NOW)["returned"] <= tools.MAX_PEOPLE


def test_a_person_carries_the_events_they_ignored(store: Path) -> None:
    """The point of the timeline. `null` means the event happened in their era and they
    did not engage with it, which is not the same as cancelling."""
    body = tools.person(store, "mem_b", now=NOW)
    assert body["lifecycle"] == "prospect"
    assert [(e["title"], e["status"]) for e in body["timeline"]] == [
        ("January", "no_show"),
        ("February", None),
        ("Next month", None),
    ]


def test_an_unknown_member_is_an_error_not_an_empty_answer(store: Path) -> None:
    """An empty timeline would be reported as "they have never attended anything",
    which is a confident and wrong answer to a mistyped id."""
    with pytest.raises(LookupError, match="no member"):
        tools.person(store, "mem_nope", now=NOW)


def test_retention_ships_the_note_that_stops_a_confident_wrong_answer(store: Path) -> None:
    """`months` minus `active_months` is the set of "no event" cells. Without that — and
    without saying so in words a model will read — a healthy community reads as
    collapsing."""
    body = tools.retention(store, now=NOW)
    assert set(body["months"]) - set(body["active_months"]) == {"2026-03", "2026-04", "2026-05", "2026-06"}
    assert "never means nobody came back" in body["note"]


def test_the_tools_are_read_only(store: Path) -> None:
    """The surface itself, asserted rather than trusted to review. A model asked about a
    community has not been asked to change it, and a tool that can write is a tool a
    model will eventually write with. Importing is the CLI's job and the app's."""
    assert {t.__name__ for t in tools.TOOLS} == {"overview", "people", "person", "retention"}
    before = connect(store, create=True)
    snapshot = (before.events(), before.members(), before.attendances(), before.thresholds())
    before.close()

    for tool in tools.TOOLS:
        try:
            tool(store, now=NOW) if tool is not tools.person else tool(store, "mem_a", now=NOW)
        except LookupError:
            pass

    after = connect(store, create=True)
    assert (after.events(), after.members(), after.attendances(), after.thresholds()) == snapshot
    after.close()


# --- the shim, which needs the optional dependency -------------------------------
def test_the_shim_names_the_install_when_the_extra_is_missing() -> None:
    """Runs everywhere, because it does not need the package: the message an operator
    gets must be an instruction, not an ImportError from three frames down."""
    from baraza.mcp.server import INSTALL_HINT

    assert "pip install" in INSTALL_HINT and "baraza[mcp]" in INSTALL_HINT


#: What each tool's schema must offer an MCP client — the tool's own arguments, and nothing
#: else. `store_path` and `now` are both **operator seams, not client arguments**: the first
#: is which calendar this session is about, the second is the clock the answer is computed
#: against. A client that could set either could read any SQLite file the process can open,
#: or ask what the community looked like on a date of its choosing.
CLIENT_ARGUMENTS = {
    "overview": set(),
    "people": {"limit"},
    "person": {"member_id"},
    "retention": set(),
}


@needs_the_extra
def test_the_server_registers_every_tool_and_nothing_else() -> None:
    """The shim is thin precisely so that what fails here is registration, never behavior."""
    from baraza.mcp.server import build

    server = build("unused.db")
    registered = {t.name for t in server._tool_manager.list_tools()}  # noqa: SLF001
    assert registered == set(CLIENT_ARGUMENTS)


@needs_the_extra
def test_every_tool_offers_exactly_the_arguments_it_documents() -> None:
    """Asserted as an equality, not an absence.

    Its predecessor checked only that ``store_path`` was *not* in the schema — which passed
    for a reason that had nothing to do with the property: `_bound` wraps every tool as
    ``def call(**kwargs)``, so FastMCP introspects **no parameters at all** and every
    schema is empty. Absence from an empty set is free. An equality cannot be satisfied
    that way: it fails both when a seam leaks out and when a real argument goes missing,
    which is the bug it was meant to catch.
    """
    from baraza.mcp.server import build

    server = build("chosen.db")
    offered = {
        tool.name: set(tool.parameters.get("properties") or {})
        for tool in server._tool_manager.list_tools()  # noqa: SLF001
    }
    assert offered == CLIENT_ARGUMENTS


def test_asking_for_no_people_returns_none(store: Path) -> None:
    """The clamp was `max(1, …)` — a floor meant to stop a negative limit
    doing something strange, which also overrode the one limit a caller might state most
    deliberately. A model asking for a count and no rows got a row."""
    answer = tools.people(store, limit=0, now=NOW)
    assert answer["people"] == []
    assert answer["returned"] == 0
    assert answer["total"] > 0
    assert answer["truncated"] is True, "asking for none of several IS a truncation, and must say so"


def test_a_negative_limit_is_not_a_request_for_rows(store: Path) -> None:
    """The reason the old floor existed, kept: below zero is a mistake, not an order."""
    assert tools.people(store, limit=-5, now=NOW)["people"] == []


def test_the_cap_still_holds_above_it(store: Path) -> None:
    """The other end. Widening the floor must not have loosened the ceiling."""
    answer = tools.people(store, limit=10_000, now=NOW)
    assert answer["returned"] <= tools.MAX_PEOPLE


def test_each_cohorts_returns_name_their_own_months(store: Path) -> None:
    """Three misreadings at once — and each of them turns a healthy
    community into a collapsing one in a summary nobody double-checks.

    The returns were a bare list, offset from the COHORT's first month while ``months`` is
    the grid's axis; so lining the two up, which is the obvious thing to do, shifted every
    cohort after the first. Its first entry was always the cohort's SIZE rather than a
    return, so every row appeared to start at 100% and fall away. And it was called
    ``returned``, which invites reading entry 0 as exactly that.
    """
    answer = tools.retention(store, now=NOW)
    for cohort in answer["cohorts"]:
        assert "returned" not in cohort, "the bare list is gone; entries name their months"
        for entry in cohort["returns"]:
            assert set(entry) == {"month", "returned", "rate"}
            assert entry["month"] in answer["months"]
        # Offset 0 is not in there: it is the cohort's size, which `size` already says.
        months = [entry["month"] for entry in cohort["returns"]]
        assert cohort["month"] not in months
        assert months == sorted(months), "a cohort's own months are in order"


def test_a_returns_entry_lands_on_the_month_the_grid_says_it_does(store: Path) -> None:
    """The alignment property itself, rather than the shape. A cohort's Nth return entry
    must name the month N after the cohort formed — asserted against the grid's own axis,
    because that is the thing the old bare list was silently offset from."""
    answer = tools.retention(store, now=NOW)
    axis = answer["months"]
    for cohort in answer["cohorts"]:
        start = axis.index(cohort["month"])
        for offset, entry in enumerate(cohort["returns"], start=1):
            assert entry["month"] == axis[start + offset]


def test_a_rate_is_a_share_of_the_cohort_and_never_invented(store: Path) -> None:
    """`rate` is the number a model will quote. It has to be the return divided by the
    cohort, and `null` where there is no return to divide — never 0, which reads as
    "nobody came back"."""
    for cohort in tools.retention(store, now=NOW)["cohorts"]:
        for entry in cohort["returns"]:
            if entry["returned"] is None:
                assert entry["rate"] is None
            else:
                assert entry["rate"] == round(entry["returned"] / cohort["size"], 3)
                assert 0.0 <= entry["rate"] <= 1.0


def test_the_note_tells_a_model_what_a_null_is_not(store: Path) -> None:
    """The one line standing between a null cell and "your community collapsed"."""
    note = tools.retention(store, now=NOW)["note"]
    assert "never means nobody came back" in note
    assert "size" in note, "the note must say where the cohort's own month went"


@needs_the_extra
def test_the_server_reports_barazas_own_version_not_the_librarys() -> None:
    """`serverInfo.version` was the `mcp` package's version, so a client
    asking what it is talking to got a number belonging to somebody else's project that
    changes whenever the optional extra is upgraded.

    Pinned here because the fix reaches through `_mcp_server`: `FastMCP` 1.x takes no
    `version` and exposes no setter. If a future release moves that field, this test is
    where it surfaces — rather than on the wire, in front of a client we cannot see.
    """
    from baraza import __version__
    from baraza.mcp.server import build

    server = build(":memory:")
    assert server._mcp_server.version == __version__


def test_the_mcp_server_refuses_an_unusable_store_before_the_transport_starts(tmp_path: Path) -> None:
    """`serve` opens the store before it serves; `mcp` did not, so a wrong `--store`
    first failed inside a tool call. That puts the error sentence in front of the MODEL
    rather than the operator who typed the command, and what they see is an assistant
    behaving oddly for no stated reason.

    Asserted by patching the transport to a tripwire: the failure must happen before
    anything runs, not surface as a bad answer once it has.
    """
    from baraza.mcp import server
    from baraza.store import StoreError

    started = False

    def _tripwire(store_path: object) -> object:
        nonlocal started
        started = True
        raise AssertionError("the transport was built despite an unusable store")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(server, "build", _tripwire)
    try:
        not_a_database = tmp_path / "notes.txt"
        not_a_database.write_text("this is prose, not a store", encoding="utf-8")
        with pytest.raises(StoreError):
            server.serve_stdio(not_a_database)

        with pytest.raises(StoreError):
            server.serve_stdio(tmp_path)  # a directory
    finally:
        monkey.undo()

    assert not started, "the store check must run before the transport is built"
