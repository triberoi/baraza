"""The command line (``baraza.cli``).

Driven through ``main(argv)`` rather than a subprocess: the same code path the console
script runs, without paying for an interpreter start per case. ``serve`` is the one
command not exercised end to end — it blocks on a socket — so what is checked there is
its wiring, and the socket itself has its own tests in ``test_web_server.py``.

The theme is that a person is reading the output. A traceback tells an organizer
nothing they can act on, and the interesting failures — a folder that is not there, a
key Luma refused — are all of that shape.
"""

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baraza.cli import DEFAULT_STORE, main, percent
from baraza.credentials import LUMA_KEY_SUFFIX, read_secret, write_secret
from baraza.store import StoredAttendance, StoredEvent, StoredMember, connect

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731

HEADER = "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "b.db"
    db = connect(path, create=True)
    db.upsert_events([StoredEvent("e1", "January", D(2026, 1, 10)), StoredEvent("e2", "February", D(2026, 2, 10))])
    db.upsert_members([StoredMember("mem_a", "amy@x.com", "Amy"), StoredMember("mem_b", "bo@x.com", "Bo")])
    db.upsert_attendances(
        [
            StoredAttendance("e1", "mem_a", "attended"),
            StoredAttendance("e2", "mem_a", "attended"),
            StoredAttendance("e1", "mem_b", "no_show"),
        ]
    )
    db.close()
    return path


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "Book_Club_-_Guests_-_2026-01-10-00-00-00.csv").write_text(
        f"{HEADER}\ng1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,,https://lu.ma/check-in/evt-zzz\n",
        encoding="utf-8",
    )
    (folder / "budget.csv").write_text("month,spend\nJanuary,120\n", encoding="utf-8")
    return folder


# --- report ------------------------------------------------------------------------
def test_report_on_an_empty_store_says_what_to_do_next(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Not an error and not an empty page. A fresh install running `report` first is
    the likeliest way anyone meets this command."""
    connect(tmp_path / "b.db", create=True).close()
    assert main(["report", "--store", str(tmp_path / "b.db")]) == 0
    assert "baraza import" in capsys.readouterr().out


def test_report_agrees_with_the_overview_screen(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Same functions underneath. A CLI that disagreed with the app about the same
    store would be worse than no CLI."""
    assert main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert "2 events, 2 people, 2 attendances" in out
    assert "67% of those who said yes" in out
    for stage in ("prospect", "first_timer", "regular", "champion", "lapsed"):
        assert stage in out


# --- import ------------------------------------------------------------------------
def test_a_missing_folder_is_a_message_and_a_failing_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A scripted caller branches on the code; a person reads the line. Both, not a
    traceback."""
    assert main(["import", str(tmp_path / "nowhere"), "--store", str(tmp_path / "b.db")]) == 1
    assert "baraza:" in capsys.readouterr().err


def test_pending_events_are_reported_and_are_not_a_failure(
    exports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolution failure falls back to manual entry, never an error. The
    exit code has to say that too, or a cron job treats a normal state as a break.

    Offline here — the resolver is a live call, and a suite that made it would be
    testing Luma's uptime.
    """
    code = main(["import", str(exports), "--store", str(tmp_path / "b.db")])
    out = capsys.readouterr().out
    assert code == 0
    assert "evt-zzz" in out and "Book Club" in out
    assert "baraza serve" in out, "the message has to say where to name them"
    assert "skipped (not a Luma export): budget.csv" in out


def test_importing_from_luma_with_no_key_anywhere_is_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connect(tmp_path / "b.db", create=True).close()
    assert main(["import", "--luma", "--store", str(tmp_path / "b.db")]) == 1
    assert "no Luma API key" in capsys.readouterr().err


def test_a_folder_and_luma_cannot_both_be_asked_for(tmp_path: Path) -> None:
    """Mutually exclusive at the parser, so the ambiguity is refused rather than
    resolved by argument order."""
    with pytest.raises(SystemExit):
        main(["import", "some-folder", "--luma", "--store", str(tmp_path / "b.db")])


def test_a_key_luma_refuses_is_never_written_to_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rule the API route holds, checked on this path too: storing before
    probing means a typo replaces the key that worked."""
    import baraza.cli as cli

    connect(tmp_path / "b.db", create=True).close()
    write_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX, "the-good-key")

    class Rejecting:
        def __init__(self, _key: str) -> None: ...
        def probe(self) -> None:
            raise RuntimeError("Luma API 401")

    monkeypatch.setattr(cli, "HttpxLumaClient", Rejecting)
    assert main(["import", "--luma", "--key", "typo", "--store", str(tmp_path / "b.db")]) == 1
    assert read_secret(tmp_path / "b.db", LUMA_KEY_SUFFIX) == "the-good-key"


# --- mcp ---------------------------------------------------------------------------
def test_mcp_without_the_extra_prints_the_install_and_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most likely error anyone meets on this CLI, and it was broken: `main`
    did not catch ``ImportError``, so a carefully written install instruction arrived as
    the last line of a traceback. Found by running the command.

    The missing package is simulated rather than assumed absent, so this test means the
    same thing in CI — where the extra *is* installed — as it does on a machine that
    cannot build it.
    """
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("mcp"):
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    connect(tmp_path / "b.db", create=True).close()

    assert main(["mcp", "--store", str(tmp_path / "b.db")]) == 1
    err = capsys.readouterr().err
    assert "pip install" in err and "baraza[mcp]" in err
    assert "Traceback" not in err


# --- the shape of the command line --------------------------------------------------
def test_the_store_defaults_to_the_working_directory() -> None:
    """Named the same way everywhere, and never guessed from a folder argument — a tool
    that puts its file somewhere you did not ask for is a tool you cannot find the file
    for."""
    assert DEFAULT_STORE == "baraza.db"


def test_every_command_takes_the_same_store_flag(tmp_path: Path) -> None:
    """Asserted across the whole surface rather than one command, so a command added
    later cannot quietly invent its own way to say where the data is."""
    from baraza.cli import _parser

    parser = _parser()
    commands = next(a for a in parser._actions if a.dest == "command")  # noqa: SLF001
    assert set(commands.choices) == {"serve", "import", "report", "mcp"}
    for name, sub in commands.choices.items():
        assert any(a.dest == "store" for a in sub._actions), f"`{name}` has no --store"  # noqa: SLF001


def test_no_command_at_all_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        main([])


PERCENT_VECTORS: list[dict[str, object]] = json.loads(
    (Path(__file__).parent / "fixtures" / "percent_vectors.json").read_text(encoding="utf-8")
)["vectors"]


def test_a_percentage_rounds_the_same_way_the_screen_does() -> None:
    """Why the fix is a shared vector file rather than a fixed number.

    ``round()`` is banker's rounding — halves go to the even neighbour, so ``round(12.5)``
    is ``12`` — while the browser's ``Math.round`` takes halves away from zero and says
    ``13``. So the CLI and the screen reported different turnout for the same store,
    which is exactly what ``_report``'s own docstring promises cannot happen.

    ``ui/src/viz.test.ts`` reads this same file. A second copy of the
    expectations is what let the divergence survive its own test.
    """
    for vector in PERCENT_VECTORS:
        assert percent(vector["rate"]) == vector["percent"], vector["why"]  # type: ignore[arg-type]


def test_the_vectors_still_contain_cases_the_two_rules_disagree_on() -> None:
    """The fixture is only a guard if it holds the cases that broke. Every value where
    half-up and banker's rounding agree would go green on the bug."""
    disagreements = [v for v in PERCENT_VECTORS if v["percent"] != round(v["rate"] * 100)]  # type: ignore[operator]
    assert len(disagreements) >= 3, "the vectors must keep covering halves that round() gets differently"


def test_a_long_event_title_says_it_was_cut(store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two differently-named events printed identically once both were cut
    at 34 characters with no marker — and this package calls silent truncation a defect
    in the MCP surface, with a test for it, and then did it here."""
    db = connect(store, create=False)
    long_name = "A Very Long Event Title That Runs Well Past The Column"
    db.upsert_events([StoredEvent("e1", long_name, D(2026, 1, 10))])
    db.close()

    assert main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert "..." in out
    assert long_name not in out  # it really was cut
    printed = next(line for line in out.splitlines() if "..." in line)
    assert long_name[:10] in printed  # ...and the part that survived is the front of it


def test_an_emoji_in_a_title_does_not_kill_the_report(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Windows piped output is often cp1252, which cannot encode an emoji;
    the write raised ``UnicodeEncodeError`` — a subclass of ``ValueError``, so ``main``'s
    handler caught it, printed a message and returned **1**. The summary had already
    flushed and the table had not, so a scripted caller was told the store was broken
    when nothing was wrong with it. Emoji in event titles are routine."""
    db = connect(store, create=False)
    db.upsert_events([StoredEvent("e1", "Rocket \U0001f680 Meetup", D(2026, 1, 10))])
    db.close()

    assert main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert "Meetup" in out
    assert "no-show" in out, "the table must reach the end, not stop at the summary"


def test_the_report_prints_dates_the_way_the_screen_does(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """This printed ``starts_at.date()``, the UTC date, while the app renders
    the local one — so an evening event west of UTC was reported on one day here and
    another there, from the same store. Both are display surfaces for the same person."""
    db = connect(store, create=False)
    late = D(2026, 1, 10, 23, 30)
    db.upsert_events([StoredEvent("e1", "Late one", late)])
    db.close()

    assert main(["report", "--store", str(store)]) == 0
    out = capsys.readouterr().out
    assert str(late.astimezone().date()) in out


def test_a_key_passed_with_a_folder_is_saved_rather_than_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--key`` alongside a folder was accepted and thrown away while
    ``--help`` said it was "saved for next time" — so the next ``--luma`` run asked for
    it again, and the organizer had put a secret in their shell history for nothing.

    The plan is explicit that the BEHAVIOR is what changes, not the help text. Saved on
    the same terms the help states: only after Luma accepts it.
    """
    import baraza.cli as cli

    class Accepting:
        def __init__(self, _key: str) -> None: ...
        def probe(self) -> None: ...

    monkeypatch.setattr(cli, "HttpxLumaClient", Accepting)
    folder = tmp_path / "exports"
    folder.mkdir()
    store_path = tmp_path / "b.db"

    assert main(["import", str(folder), "--key", "a-good-key", "--store", str(store_path)]) == 0
    assert read_secret(store_path, LUMA_KEY_SUFFIX) == "a-good-key"
    assert "saved for next time" in capsys.readouterr().out


def test_a_key_luma_refuses_is_not_saved_on_the_folder_path_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the promise — "and only after Luma accepts it" — which
    a fix that simply wrote the key would have broken while appearing to honour the
    help."""
    import baraza.cli as cli

    class Rejecting:
        def __init__(self, _key: str) -> None: ...
        def probe(self) -> None:
            raise RuntimeError("Luma API 401")

    monkeypatch.setattr(cli, "HttpxLumaClient", Rejecting)
    folder = tmp_path / "exports"
    folder.mkdir()
    store_path = tmp_path / "b.db"

    assert main(["import", str(folder), "--key", "typo", "--store", str(store_path)]) == 1
    assert read_secret(store_path, LUMA_KEY_SUFFIX) is None


# --- the mirror itself ---------------------------------------------------------------
VIZ_TS = Path(__file__).parents[1] / "ui" / "src" / "viz.ts"

#: `export const percent = (value: number): string => `${Math.round(value * 100)}%``
_TS_PERCENT_RE = re.compile(
    r"export const percent\s*=\s*\(value: number\): string =>\s*`\$\{(?P<body>[^}]+)\}%`"
)


def test_the_typescript_percent_is_the_same_rule_as_this_one() -> None:
    """The guard that always runs, in the shape ``test_cross_language_constants.py``
    uses: read the TypeScript source and hold it to the rule Python implements.

    ``Math.round(value * 100)`` IS ``floor(value * 100 + 0.5)`` for non-negative
    values, which is every rate here. This asserts the EXPRESSION rather than the
    output, so a change to either side that would reintroduce the divergence fails
    here — which a hardcoded expectation cannot give, because its test compared
    against a hardcoded string on one side only.
    """
    match = _TS_PERCENT_RE.search(VIZ_TS.read_text(encoding="utf-8"))
    assert match is not None, f"could not find `percent` in {VIZ_TS.name} — did it move or change shape?"
    assert match.group("body").strip() == "Math.round(value * 100)", (
        "the browser's rounding changed; Python's `percent` must change with it or the "
        "CLI and the screen will disagree again"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_real_javascript_agrees_on_every_vector() -> None:
    """The stronger check when a JS engine is available: run the ACTUAL expression from
    ``viz.ts`` over the shared vectors, rather than reasoning that it is equivalent.

    Skipped without node, which is why the source-shape test above exists and is not
    conditional — a guard that only runs on some machines is not a guard.
    """
    body = _TS_PERCENT_RE.search(VIZ_TS.read_text(encoding="utf-8")).group("body")  # type: ignore[union-attr]
    rates = [v["rate"] for v in PERCENT_VECTORS]
    script = f"const f = (value) => {body}; console.log(JSON.stringify({json.dumps(rates)}.map(f)))"
    completed = subprocess.run(  # noqa: S603 — a literal we just built, no shell
        [shutil.which("node") or "node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(completed.stdout) == [v["percent"] for v in PERCENT_VECTORS]


def test_a_folder_with_no_luma_exports_in_it_says_so_rather_than_reporting_a_clean_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pointing at the wrong folder is the first mistake a new organizer
    makes — the README sends them at their Downloads folder, which on a real machine holds
    hundreds of files — and it used to be indistinguishable from a successful import of
    nothing: "0 events, 0 people, 0 registrations." and a success exit code.

    A non-CSV is filtered by suffix before anything looks at it, so it never reaches
    ``files_skipped`` either; the mechanism was already understood in ``importer.py``
    (an organizer looking at "0 events" has nothing to go on) and only the glob had been
    fixed.
    """
    empty = tmp_path / "not-exports"
    empty.mkdir()
    (empty / "notes.txt").write_text("nothing to do with Luma", encoding="utf-8")

    assert main(["import", str(empty), "--store", str(tmp_path / "b.db")]) == 0

    out = capsys.readouterr().out
    assert "No Luma guest-list exports" in out
    assert str(empty) in out
def test_the_luma_key_can_come_from_the_environment_instead_of_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--key` was the only way in, so the organizer's credential landed in
    their shell history and in the process table, where any other account on the machine
    can read it with `ps`. The project already uses this variable name in
    `scripts/dev/capture_luma_sample.py`.

    Asserted by the failure it does NOT produce: with the variable set, the run gets far
    enough to try Luma rather than refusing for want of a key.
    """
    monkeypatch.setenv("LUMA_API_KEY", "secret-from-the-environment")
    code = main(["import", "--luma", "--store", str(tmp_path / "b.db")])
    assert "no Luma API key" not in capsys.readouterr().err
    assert code == 1  # it reached the network and failed there, which is the point


def test_a_key_from_the_environment_is_not_written_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--key` means "save this for next time" and says so in the help. An environment
    variable is the opposite promise: the caller is supplying it per-run, and writing it
    into the store's folder would put a credential somewhere they did not ask for."""
    monkeypatch.setenv("LUMA_API_KEY", "secret-from-the-environment")
    store = tmp_path / "b.db"
    main(["import", "--luma", "--store", str(store)])
    assert read_secret(store, LUMA_KEY_SUFFIX) is None


def test_offline_reads_a_folder_without_asking_luma_anything(
    exports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The README's "Nothing is uploaded" was not true of the free path:
    every event the store has not seen is looked up against Luma's public endpoint to
    fill in its name and date. No guest data leaves — but for a tool whose whole pitch is
    that it runs on your machine, the claim has to be one an organizer can actually make.

    Proven at the socket rather than by patching the resolver: `import_folder` binds it as
    a default argument, so a module-level patch would not be seen and the test would pass
    without proving anything.
    """
    import httpx

    def refuse(*_a: object, **_k: object) -> object:
        raise AssertionError("--offline must not reach the network")

    monkeypatch.setattr(httpx.Client, "send", refuse)

    assert main(["import", str(exports), "--offline", "--store", str(tmp_path / "b.db")]) == 0
    assert "events" in capsys.readouterr().out


def test_without_offline_the_folder_route_still_resolves_event_names(
    exports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: `--offline` has to be the difference, not a flag that changes
    nothing. Same corpus, same store, no flag — and the network IS reached."""
    import httpx

    reached: list[str] = []

    def record(self: object, request: object, **_k: object) -> object:  # noqa: ANN401
        reached.append(str(getattr(request, "url", "")))
        # A transport failure rather than an AssertionError: the resolver is documented
        # never to raise, so this exercises the real path and leaves `main()` free to
        # finish. The attempt itself is the assertion.
        raise httpx.ConnectError("blocked by the test")

    monkeypatch.setattr(httpx.Client, "send", record)

    main(["import", str(exports), "--store", str(tmp_path / "b.db")])
    assert reached, "the folder route should have asked Luma to name its events"
