"""The walk itself.

These are the defects a browser finds and a unit test cannot: a screen you can reach
once and never again, a form that erases the list it belongs to, a link that blanks the
page. Each test below names the finding it came from, so a green run says which
discoveries it is now confirming rather than repeating.

Kept small on purpose. This is not a second test suite for behavior that
``tests/`` already covers over a far wider input space — it is the layer
those tests structurally cannot see, which is what happens in a browser.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.usefixtures("served")


#: The Import screen's folder field, whose placeholder is a Windows-shaped path.
PLACEHOLDER = "C:\\Users\\you\\Downloads"


def test_the_five_screens_are_reachable_and_say_something(page: Page, served) -> None:  # noqa: ANN001
    """The spine. If this fails, nothing below is worth reading."""
    page.goto(served.url)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()

    page.get_by_role("link", name="People").click()
    expect(page.get_by_text("Cam Champion")).to_be_visible()

    page.get_by_text("Cam Champion").click()
    expect(page.get_by_role("heading", name="Cam Champion")).to_be_visible()

    page.get_by_role("link", name="Returning").click()
    expect(page.get_by_text("Repeat attendance by cohort")).to_be_visible()


def test_import_is_reachable_once_there_is_data(page: Page, served) -> None:  # noqa: ANN001
    """The largest item on the review, and only a browser finds it.

    The import screen was the one place in the app that can read files, and it rendered
    only when the store was empty. The nav had three entries and none of them was it. So
    the tool's core loop — drop next month's export in the folder, press refresh — became
    unreachable the moment the first import succeeded, and the only way back was deleting
    the data file.

    A unit test cannot see this: every endpoint involved answers correctly. What was
    missing was a route to them.
    """
    page.goto(served.url)
    page.get_by_role("link", name="Import").click()
    expect(page.get_by_role("heading", name="Bring in your Luma events")).to_be_visible()
    expect(page.get_by_placeholder("C:\\Users\\you\\Downloads")).to_be_visible()


def test_a_folder_that_is_not_there_is_a_sentence_on_the_screen(page: Page, served) -> None:  # noqa: ANN001
    """The error path an organizer actually hits, end to end through the browser: the
    422/400 has to arrive as words on the import screen rather than a blank panel."""
    page.goto(f"{served.origin}/import?token={served.url.split('token=')[1]}")
    page.get_by_placeholder("C:\\Users\\you\\Downloads").fill("/no/such/folder/anywhere")
    page.get_by_role("button", name="Read this folder").click()
    expect(page.get_by_text("That did not work")).to_be_visible()


def test_an_empty_store_lands_on_import_rather_than_four_zeroes(page: Page, served_empty) -> None:  # noqa: ANN001
    """The other end of it: before anything is imported the import screen is
    still the right landing, because an empty overview is four zeroes and three empty
    charts and teaches nothing."""
    page.goto(served_empty.url)
    expect(page.get_by_role("heading", name="Bring in your Luma events")).to_be_visible()


def test_a_malformed_link_says_something_instead_of_blanking(page: Page, served) -> None:  # noqa: ANN001
    """Address decoding ran unguarded during the first render, so a
    malformed percent-escape threw before anything mounted and the organizer got a white
    page with nothing on it — no error, no nav, no way back."""
    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/people/%E0%A4%A?token={token}")
    expect(page.locator("body")).not_to_be_empty()
    # The wordmark, by its accessible name rather than by text: it is an image now, and
    # asserting the name is what proves a screen reader still gets the app's identity.
    expect(page.get_by_role("img", name="baraza")).to_be_visible()


def test_a_member_who_does_not_exist_is_not_blamed_on_the_store(page: Page, served) -> None:  # noqa: ANN001
    """Every failed load was headlined "baraza could not read your
    store", including a member id that simply is not there. The store is fine; the
    address is wrong, and the advice for the two is not the same."""
    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/people/mem_nobody?token={token}")
    expect(page.get_by_role("alert")).to_be_visible()
    expect(page.get_by_text("could not read your store")).to_have_count(0)


def test_the_session_message_says_to_reopen_the_link(page: Page, served) -> None:  # noqa: ANN001
    """The other half of it: an expired or missing token is the single most
    likely way an organizer gets stuck, and the right advice is "reopen the link from
    your terminal" — not a claim about their data."""
    page.goto(served.origin)
    expect(page.get_by_text("Open the link from your terminal")).to_be_visible()


def test_naming_one_pending_event_keeps_the_others_on_screen(page: Page, served, tmp_path: Path) -> None:  # noqa: ANN001
    """Only visible in a browser, because it is about a screen
    unmounting.

    Saving a name triggered a status reload, the reload dropped the whole screen back to
    "Reading your store…", and everything the import had reported went with it: the
    skipped-file list, the remaining unnamed events, and the confirmation that the save
    had worked. The organizer named one event and watched the rest disappear.
    """
    # One file per event: a Luma guest-list export IS per-event, and the parser takes the
    # id from the first row that has one. Two events therefore need two files.
    #
    # Ids are alphanumeric after `evt-` because that is what the parser's regex accepts
    # (`evt-[A-Za-z0-9]+`). A hyphenated id is cut at the hyphen, so `evt-unknown-one`
    # and `evt-unknown-two` both arrive as `evt-unknown` — one event, not the two this
    # test needs.
    folder = tmp_path / "exports"
    folder.mkdir()
    header = "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
    (folder / "one.csv").write_text(
        header + "g1,Ada,ada@example.com,2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-unknownONE\n",
        encoding="utf-8",
    )
    (folder / "two.csv").write_text(
        header + "g2,Bea,bea@example.com,2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-unknownTWO\n",
        encoding="utf-8",
    )

    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/import?token={token}")
    page.get_by_placeholder("C:\\Users\\you\\Downloads").fill(str(folder))
    page.get_by_role("button", name="Read this folder").click()

    # Two events came in unnamed; both must be offered.
    expect(page.get_by_text("evt-unknownONE")).to_be_visible()
    expect(page.get_by_text("evt-unknownTWO")).to_be_visible()

    rows = page.locator(".pending__row")
    first = rows.filter(has_text="evt-unknownONE")
    first.get_by_placeholder("Event name").fill("The first one")
    first.locator("input[type=date]").fill("2026-05-01")
    first.get_by_role("button", name="Save").click()

    # The confirmation is visible...
    expect(first.get_by_text("Saved")).to_be_visible()
    # ...and the other event is still there to name.
    expect(page.get_by_text("evt-unknownTWO")).to_be_visible()
    expect(rows.filter(has_text="evt-unknownTWO").get_by_role("button", name="Save")).to_be_visible()


def test_the_pending_list_survives_a_reload(page: Page, served, tmp_path: Path) -> None:  # noqa: ANN001
    """The 2026-08-16 first-impression walk's worst finding, as a walk.

    The pending list rendered only from the import response, so it existed exactly
    once: a reload — or arriving from the CLI, whose output says "name them in the
    app" — found an Import screen with no sign that work was waiting. It now comes from
    the store, and the folder field offers the last-read folder back, because naming an
    event only finishes when that folder is read again.
    """
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "one.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Ada,ada@example.com,2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-unknownONE\n",
        encoding="utf-8",
    )

    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/import?token={token}")
    page.get_by_placeholder(PLACEHOLDER).fill(str(folder))
    page.get_by_role("button", name="Read this folder").click()
    expect(page.get_by_text("evt-unknownONE")).to_be_visible()

    # The reload is the test. Before, this left a screen with no pending list at all.
    page.goto(f"{served.origin}/import?token={token}")
    expect(page.get_by_text("evt-unknownONE")).to_be_visible()
    expect(page.get_by_placeholder(PLACEHOLDER)).to_have_value(str(folder.resolve()))


def test_naming_an_event_and_rereading_finishes_with_a_way_forward(page: Page, served_empty, tmp_path: Path) -> None:  # noqa: ANN001
    """The whole first-run loop, plus its exit. The import report used to end at
    "1 event, 1 person" with nothing to click — the nav appearing top-right is not
    something a first-time organizer knows to look for."""
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "one.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Ada,ada@example.com,2026-05-01T09:00:00Z,approved,2026-05-01T18:30:00Z,"
        "https://lu.ma/check-in/evt-unknownONE\n",
        encoding="utf-8",
    )

    page.goto(served_empty.url)
    page.get_by_placeholder(PLACEHOLDER).fill(str(folder))
    page.get_by_role("button", name="Read this folder").click()

    row = page.locator(".pending__row").filter(has_text="evt-unknownONE")
    row.get_by_placeholder("Event name").fill("Ada's night")
    row.locator("input[type=date]").fill("2026-05-01")
    row.get_by_role("button", name="Save").click()
    expect(row.get_by_text("Saved")).to_be_visible()

    page.get_by_role("button", name="Read this folder").click()
    expect(page.get_by_text("1 event, 1 person")).to_be_visible()
    page.get_by_role("link", name="See your overview").click()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()


def test_settings_are_editable_and_survive_a_reload(page: Page, served) -> None:  # noqa: ANN001
    """The screen the API's own error copy promised ("Open Settings and save a set of
    thresholds") while the app had no Settings to open."""
    page.goto(served.url)
    page.get_by_role("link", name="Settings").click()
    expect(page.get_by_role("heading", name="Settings")).to_be_visible()

    lapsed = page.locator(".settings__field").filter(has_text="Lapsed after").locator("input")
    lapsed.fill("45")
    page.get_by_role("button", name="Save").click()
    expect(page.get_by_text("Saved.")).to_be_visible()

    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/settings?token={token}")
    expect(page.locator(".settings__field").filter(has_text="Lapsed after").locator("input")).to_have_value("45")


#: axe-core, from the UI's own devDependencies — the same tree `npm ci` builds the app
#: from, so CI has it wherever it can build the UI at all. Injected as a file, never
#: fetched: the walkthrough must not phone a CDN any more than the app may.
AXE_JS = Path(__file__).parents[1] / "ui" / "node_modules" / "axe-core" / "axe.min.js"


@pytest.mark.parametrize("path", ["/", "/people", "/returning", "/import", "/settings"])
def test_axe_finds_no_wcag_a_or_aa_violations(page: Page, served, path: str) -> None:  # noqa: ANN001
    """The floor under the 2026-08-16 accessibility audit, held mechanically.

    The audit found the chart palette thoroughly tested and the CHROME not tested at
    all — muted text sat at 2.36:1 on every screen. `tests/test_palette_contrast.py`
    now scores the token pairs; this pass is the backstop for everything a token test
    cannot see (a hardcoded color in a component, a control without a name, contrast on
    a state a static read misses), on every screen, in the rendered page.
    """
    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}{path}?token={token}")
    # Definite content, not just a mounted shell: axe on a still-loading screen scans
    # a spinner. Every screen renders an h1 once its data arrives.
    expect(page.locator("h1").first).to_be_visible()

    page.add_script_tag(path=str(AXE_JS))
    result = page.evaluate(
        "() => axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] } })"
    )
    violations = [
        f"{v['id']} ({v['impact']}): {v['help']} — {len(v['nodes'])} node(s), e.g. "
        + (v["nodes"][0]["target"][0] if v["nodes"] else "?")
        for v in result["violations"]
    ]
    assert not violations, f"axe on {path}:\n  " + "\n  ".join(violations)


def test_a_cohorts_own_month_is_not_reported_as_a_return(page: Page, served) -> None:  # noqa: ANN001
    """Column 0 is the cohort's size by construction — everyone attended
    in the month they first attended — so "12 of 12 came back" for that cell is a
    definition dressed up as a measurement, and it is the cell a reader anchors the whole
    row against."""
    page.goto(f"{served.origin}/returning?token={served.url.split('token=')[1]}")
    expect(page.get_by_text("Repeat attendance by cohort")).to_be_visible()
    assert "came back" not in (page.locator(".grid tbody tr").first.locator("td").nth(1).get_attribute("title") or "")


def test_the_first_import_keeps_its_report_when_the_app_stops_being_empty(
    page: Page,
    served_empty,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    """Exists only in a browser — every endpoint
    answered correctly throughout.

    On a first run the Import screen renders at ``/`` because the store is empty. The
    moment an import first SUCCEEDS, ``has_data`` flips, the app falls through to the
    routed tree with ``route.name`` still ``overview``, and React unmounts the screen
    mid-flow — taking the report with it: the counts, the skipped files, and the events
    still needing a name before their guests can be used.

    The route the organizer actually walks: three unnamed events arrive (nothing has a
    date, so nothing imports and the store stays empty), they name one, and they read the
    folder again — which is what the screen tells them to do. THAT read is the first one
    to return an event, so that is where the flip happens, and where two events that still
    need names used to disappear behind a confident Overview.

    ``test_naming_one_pending_event_keeps_the_others_on_screen`` cannot see this: it uses
    the ``served`` fixture, whose store already has data, so ``has_data`` is true before
    it starts and the flip never happens.
    """
    folder = tmp_path / "exports"
    folder.mkdir()
    header = "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
    guests = (
        ("ONE", "g1,Ada,ada@example.com"),
        ("TWO", "g2,Bea,bea@example.com"),
        ("THREE", "g3,Cy,cy@example.com"),
    )
    for tag, guest in guests:
        (folder / f"{tag.lower()}.csv").write_text(
            header + f"{guest},2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-unknown{tag}\n",
            encoding="utf-8",
        )

    token = served_empty.url.split("token=")[1]
    page.goto(f"{served_empty.origin}/?token={token}")

    def read_the_folder() -> object:
        """Click Read, and wait for the import RESPONSE — not for a redraw, and not for a
        fixed delay. Both of those let an assertion run against the DOM the app is about
        to replace, which is how a first attempt at this test passed against the broken
        build."""
        page.get_by_placeholder(PLACEHOLDER).fill(str(folder))
        with page.expect_response(lambda r: "/api/import/folder" in r.url) as caught:
            page.get_by_role("button", name="Read this folder").click()
        return caught.value.json()

    expect(page.get_by_placeholder(PLACEHOLDER)).to_be_visible()
    first_report = read_the_folder()
    assert first_report["events"] == 0, first_report  # nothing has a date yet

    rows = page.locator(".pending__row")
    expect(rows).to_have_count(3)

    # Name one, and wait for the STORE to say it took. Asserting the "Saved" confirmation
    # is not enough: naming deliberately leaves the row on screen, so the
    # list looks identical either way, and a save that silently did nothing would leave
    # the read below importing nothing and the flip never happening — a test that passes
    # by not reaching its own subject.
    first = rows.filter(has_text="evt-unknownONE")
    first.get_by_placeholder("Event name").fill("Founders Coffee")
    first.locator("input[type=date]").fill("2026-05-01")
    first.get_by_role("button", name="Save").click()
    expect(first.get_by_text("Saved")).to_be_visible()

    # Read the folder again — what the screen tells them to do, and the first read that
    # returns an event. This is the flip, and asserting it happened is what stops this
    # test passing by never reaching its own subject.
    second_report = read_the_folder()
    assert second_report["events"] == 1, second_report
    assert len(second_report["pending"]) == 2, second_report

    # The heading first, and it carries the whole finding: the app used to fall straight
    # through to Overview here, because the URL still said `/` while the Import screen was
    # rendering. `to_have_text` retries, so this waits out the re-render rather than
    # racing it — checking `has_data` alone passed against the broken build, since the API
    # flips before React has unmounted anything.
    expect(page.locator("h1").first).to_have_text("Bring in your Luma events")

    # ...and the two events that still need names survived it.
    expect(page.get_by_text("evt-unknownTWO")).to_be_visible()
    expect(page.get_by_text("evt-unknownTHREE")).to_be_visible()
    expect(rows.filter(has_text="evt-unknownTWO").get_by_role("button", name="Save")).to_be_visible()


def test_no_screen_scrolls_sideways_on_a_narrow_phone(page: Page, served) -> None:  # noqa: ANN001
    """The nav could not fit beside the wordmark under about 394px, so the
    DOCUMENT grew wider than the viewport — every screen scrolled sideways, and the dark
    header, being only as wide as the viewport, left a white stripe down the right of the
    page for its full height.

    Measured rather than eyeballed, and on the real viewport rather than in a sized frame:
    a hidden or throttled render reports confident wrong numbers.
    """
    page.set_viewport_size({"width": 375, "height": 812})
    token = served.url.split("token=")[1]

    for path in ("/", "/people", "/returning", "/import"):
        page.goto(f"{served.origin}{path}?token={token}")
        widths = page.evaluate(
            "() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth })"
        )
        assert widths["scroll"] <= widths["client"], f"{path} overflows: {widths}"


def test_enter_submits_the_folder_field(page: Page, served, tmp_path: Path) -> None:  # noqa: ANN001
    """The inputs sat in a `<label>` with no `<form>` around them, so typing
    a path and pressing Enter did nothing at all — no request, no message, no hint that
    the button was the only way through."""
    folder = tmp_path / "exports"
    folder.mkdir()

    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/import?token={token}")

    page.get_by_placeholder(PLACEHOLDER).fill(str(folder))
    with page.expect_response(lambda r: "/api/import/folder" in r.url) as caught:
        page.get_by_placeholder(PLACEHOLDER).press("Enter")

    assert caught.value.status == 200


def test_a_failed_import_clears_the_previous_run_s_report(page: Page, served, tmp_path: Path) -> None:  # noqa: ANN001
    """`run()` cleared the problem on entry but never the report, so after a
    failure the last successful run's report — including its live Save form — sat directly
    under "That did not work", telling the organizer to read a folder that was no longer
    in the box."""
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "one.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url" + chr(10)
        + "g1,Ada,ada@example.com,2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-staleONE" + chr(10),
        encoding="utf-8",
    )

    token = served.url.split("token=")[1]
    page.goto(f"{served.origin}/import?token={token}")

    page.get_by_placeholder(PLACEHOLDER).fill(str(folder))
    with page.expect_response(lambda r: "/api/import/folder" in r.url):
        page.get_by_role("button", name="Read this folder").click()
    expect(page.locator("section.report")).to_be_visible()

    page.get_by_placeholder(PLACEHOLDER).fill(str(tmp_path / "definitely-not-here"))
    with page.expect_response(lambda r: "/api/import/folder" in r.url):
        page.get_by_role("button", name="Read this folder").click()

    expect(page.get_by_text("That did not work")).to_be_visible()
    expect(page.locator("section.report")).to_have_count(0)


def test_the_application_funnel_is_on_screen_with_its_caveat(page: Page, served) -> None:  # noqa: ANN001
    """The columns and the sentence that qualifies them, in a real browser.

    The counts read as more precise than the export behind them — Luma uses one word for
    an organizer declining an application and a guest saying they are not coming — so the
    qualification is part of the feature, not documentation of it. A tooltip alone would
    leave it unread; this asserts the visible note, which is what a reader cannot miss.
    """
    page.goto(served.url)
    page.get_by_text("Applications as a table").click()

    expect(page.get_by_role("columnheader", name="Applied")).to_be_visible()
    expect(page.get_by_role("columnheader", name="Awaiting a decision")).to_be_visible()
    expect(page.get_by_role("columnheader", name="Declined or withdrew")).to_be_visible()

    note = page.get_by_text("is one word in Luma for two different things")
    expect(note).to_be_visible()

    # Scoped to the applications table by name. Unscoped, "March supper club" also matches
    # the turnout table's row for the same event, and the assertion silently reads that
    # row's numbers instead — which is exactly what it did first.
    applications = page.locator("table").filter(has=page.get_by_role("columnheader", name="Applied"))
    row = applications.get_by_role("row").filter(has_text="March supper club")
    # `to_be_visible` passes for a cell scrolled out of an overflow container, so it cannot
    # answer "can a reader see this". The funnel's first home was three extra columns on the
    # turnout table: they rendered correctly, passed `to_be_visible`, and sat off the right
    # edge of a half-width `overflow-x: auto` box where nobody would find them. The last
    # column being in the viewport once the row is scrolled to is the property that failed.
    row.scroll_into_view_if_needed()
    expect(row.get_by_role("cell").nth(-1)).to_be_in_viewport()
    expect(row.get_by_role("cell").nth(-3)).to_have_text("3")
    expect(row.get_by_role("cell").nth(-2)).to_have_text("1")
    expect(row.get_by_role("cell").nth(-1)).to_have_text("1")


def test_an_open_event_shows_a_dash_not_a_zero_in_the_funnel(page: Page, served) -> None:  # noqa: ANN001
    """An event nobody could apply to has no funnel, which is not a funnel of zero.
    Printed as an em dash for the same reason a show rate with nobody to count is."""
    page.goto(served.url)
    page.get_by_text("Applications as a table").click()

    applications = page.locator("table").filter(has=page.get_by_role("columnheader", name="Applied"))
    row = applications.get_by_role("row").filter(has_text="January meetup")
    expect(row.get_by_role("cell").nth(-3)).to_have_text("—")
    expect(row.get_by_role("cell").nth(-2)).to_have_text("—")
    expect(row.get_by_role("cell").nth(-1)).to_have_text("—")
