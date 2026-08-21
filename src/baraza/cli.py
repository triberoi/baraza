"""``baraza`` — the command line.

``serve`` starts the local app and prints the link, which is what almost everybody uses.
``import`` and ``report`` are for scripting: a cron job re-reading a folder, a summary
piped somewhere else.

argparse rather than a CLI framework: four commands do not justify a dependency an
organizer has to install to read their own calendar.

``--store`` defaults to ``baraza.db`` in the current directory, so the artifact lands
beside the exports. Nothing guesses a path from a folder argument — a tool that puts its
file somewhere you did not ask for is one you cannot find the file for.
"""

from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import webbrowser
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import uvicorn

from baraza.analytics import REGISTRATION_ONLY, analyze_members
from baraza.analytics.views import event_attendance, lifecycle_mix
from baraza.credentials import LUMA_KEY_SUFFIX, CredentialError, read_secret, write_secret
from baraza.importer import import_folder, import_luma_api
from baraza.ingest import HttpxLumaClient
from baraza.store import StoreError, connect
from baraza.web import prepare

DEFAULT_STORE = "baraza.db"

#: How wide an event title may print before it is cut. Narrow enough that the columns
#: after it line up on an 80-column terminal.
TITLE_WIDTH = 34

#: What a cut title ends with. ASCII on purpose: this is the one marker that must
#: survive a terminal encoding that cannot carry "…", and a truncation marker that
#: itself gets mangled is worse than none.
ELLIPSIS = "..."


def percent(rate: float) -> int:
    """A rate in ``[0, 1]`` as a whole percentage, rounding halves UP.

    Half-up, not ``round()``. Python rounds halves to the even neighbour and JavaScript
    rounds them away from zero, so the CLI and the screen print different turnout for the
    same store. The browser's behaviour is the one kept, and ``floor(x + 0.5)`` is
    ``Math.round`` for the non-negative values this ever sees.

    Held to the TypeScript by a shared vector file, not a hardcoded expected string.
    """
    return math.floor(rate * 100 + 0.5)


def _fit(title: str) -> str:
    """A title cut to :data:`TITLE_WIDTH`, marked when it was cut — silent truncation makes
    two differently-named events print identically."""
    if len(title) <= TITLE_WIDTH:
        return title
    return title[: TITLE_WIDTH - len(ELLIPSIS)] + ELLIPSIS


def _printable_output() -> None:
    """Let the report survive a console that cannot encode what is in the data.

    An emoji in an event title is routine and Windows piped output is often cp1252.
    Without this the write raises ``UnicodeEncodeError``, which :func:`main` catches as a
    ``ValueError`` and turns into exit 1 — telling a scripted caller the store is broken
    when nothing is wrong with it. A substitution character costs a glyph; the alternative
    costs the rest of the report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # a plain TextIOWrapper; not so under some captures
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):  # pragma: no cover — a stream that refuses
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than raising, so a scripted
    caller gets something it can branch on."""
    _printable_output()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.run(args))
    except (
        OSError,
        ValueError,
        LookupError,
        RuntimeError,
        ImportError,
        StoreError,
        CredentialError,
        sqlite3.DatabaseError,
    ) as exc:
        # The message is for a person at a terminal, and the interesting failures — a
        # missing folder, a refused key, a --store that is not a database, a store from a
        # newer baraza — are all of this shape. Keep the list wide: an uncaught one arrives
        # as a wall of traceback with the useful sentence as its last line.
        #
        # `ImportError` covers `baraza mcp` without the optional extra, which is the
        # likeliest error anyone meets on this CLI. Without it the install instruction
        # arrives as the last line of a traceback.
        print(f"baraza: {exc}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="baraza", description="Attendance analytics for Luma calendars.")
    subs = parser.add_subparsers(dest="command", required=True)

    def with_store(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--store", default=DEFAULT_STORE, help=f"the artifact to use (default: {DEFAULT_STORE})")
        return sub

    serve = with_store(subs.add_parser("serve", help="start the local app and open it"))
    serve.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    serve.set_defaults(run=_serve)

    read = with_store(subs.add_parser("import", help="read Luma exports, or the Luma API"))
    source = read.add_mutually_exclusive_group(required=True)
    source.add_argument("folder", nargs="?", help="a folder of Luma guest-list CSV exports")
    source.add_argument("--luma", action="store_true", help="read the calendar from the Luma API instead")
    read.add_argument("--key", help="Luma API key; saved for next time, and only after Luma accepts it")
    read.add_argument(
        "--offline",
        action="store_true",
        help="do not ask Luma to name events; read the folder and nothing else",
    )
    read.set_defaults(run=_import)

    report = with_store(subs.add_parser("report", help="print a summary of the store"))
    report.set_defaults(run=_report)

    mcp = with_store(subs.add_parser("mcp", help="run the MCP server on stdio"))
    mcp.set_defaults(run=_mcp)

    return parser


def _serve(args: argparse.Namespace) -> int:
    """Start the app. The URL carries the session token, which is why it is printed
    whether or not a browser opens — the organizer needs it if the browser does not."""
    url, sock, app = prepare(args.store)
    print(f"baraza is reading {Path(args.store).resolve()}")
    print(f"\n    {url}\n")
    print("That link carries this session's key. Ctrl-C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
    return 0


def _import(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    # The one command that legitimately brings a store into existence.
    store = connect(args.store, create=True)
    try:
        if args.luma:
            # `--key` first (this run, and save it), then the environment (this run,
            # do NOT save it), then whatever was saved earlier. The environment variable
            # exists because the flag was the only way in, which put the organizer's
            # credential in their shell history and in the process table, where any other
            # account on the machine can read it with `ps`. It is deliberately not
            # written to disk: `--key` promises "saved for next time" and says so in the
            # help, and an environment variable promises the opposite.
            from_env = os.environ.get("LUMA_API_KEY")
            key = args.key or from_env or read_secret(args.store, LUMA_KEY_SUFFIX)
            if not key:
                raise ValueError(
                    "no Luma API key — pass --key once, set LUMA_API_KEY, or import a folder instead"
                )
            client = HttpxLumaClient(key)
            # Probed before it is stored, exactly as the API route does: storing first
            # means a typo replaces the key that worked.
            client.probe()
            if args.key:
                write_secret(args.store, LUMA_KEY_SUFFIX, args.key)
            result = import_luma_api(store, client, now=now)
        else:
            if args.key:
                # `--help` promises the key is saved for next time, so honor it on this
                # path too, on the same terms: probed first, so a typo is refused rather
                # than stored. Accepting it and discarding it would put a secret in the
                # organizer's shell history for nothing.
                probe_client = HttpxLumaClient(args.key)
                probe_client.probe()
                write_secret(args.store, LUMA_KEY_SUFFIX, args.key)
                print("Luma accepted that key; saved for next time.")
            # `--offline` withholds the resolver, which is the only thing on this route
            # that talks to anyone. Event names and dates then have to be typed in on the
            # Import screen, which is the trade the flag exists to offer.
            result = (
                import_folder(store, args.folder, now=now, resolve=None)
                if args.offline
                else import_folder(store, args.folder, now=now)
            )
    finally:
        store.close()

    # A folder the glob found nothing in is not a successful import of nothing, and the
    # counts alone cannot tell the two apart. Said before them, because
    # "0 events, 0 people" reads as a result and this is the reason there isn't one.
    # Folder path only: `--luma` has no folder and `files_read` stays at its default.
    if not args.luma and result.files_read == 0:
        print(f"No Luma guest-list exports (*.csv) in {Path(args.folder).resolve()}")
        print("Check the folder — a Luma export is a .csv you download from an event page.")
    print(f"{result.events} events, {result.members} people, {result.attendances} registrations.")
    for skipped in result.files_skipped:
        print(f"  skipped (not a Luma export): {skipped}")
    for warning in result.warnings:
        print(f"  note: {warning}")
    if result.pending:
        # Not an error, and the exit code says so. These need a name and a date, which
        # the app's first-run screen asks for; the CLI reports them rather than
        # inventing a second way to answer.
        print(f"\n{len(result.pending)} events need a name and a date before their guests can be used:")
        for event in result.pending:
            title = event.inferred_title or "(no title in the filename)"
            print(f"  {event.luma_event_id}  {event.guests} guests  {title}")
        print("\nName them in the app (`baraza serve`), then run this again.")
        print("Keep the folder: that second read is what brings their guests in.")
    return 0


def _report(args: argparse.Namespace) -> int:
    """A summary, for piping. Deliberately the same numbers the overview screen shows —
    a CLI that disagreed with the app about the same store would be worse than no CLI."""
    now = datetime.now(UTC)
    store = connect(args.store, read_only=True)
    try:
        events, attendances = store.calendar()
        thresholds = store.thresholds()
    finally:
        store.close()

    if not attendances:
        print("Nothing imported yet. Try: baraza import <folder>")
        return 0

    members = analyze_members(events, attendances, now=now, thresholds=thresholds)
    turnout = event_attendance(events, attendances, now=now)
    attended = sum(row.attended for row in turnout)
    # The rate is summed over the MEASURED events only. An event the organizer declared
    # registration-only reads every committed guest as attended, so including it would add
    # a 100% to both halves and pull the figure toward one — reporting a turnout nobody
    # took, and making the events that were scanned look worse beside it.
    measured = [row for row in turnout if row.show_rate is not None]
    unmeasured = len(turnout) - len(measured)
    attended_measured = sum(row.attended for row in measured)
    committed = sum(row.committed for row in measured)

    print(f"{len(turnout)} events, {len(members)} people, {attended} attendances")
    if committed:
        # The scope goes on the same line as the number whenever it is not the whole
        # calendar.
        scope = f" (measured across {len(measured)} of {len(turnout)} events)" if unmeasured else ""
        print(f"turned up: {percent(attended_measured / committed)}% of those who said yes{scope}")
    else:
        print("turned up: — (no event recorded attendance)" if unmeasured else "turned up: —")
    print()
    for stage, count in lifecycle_mix(members).items():
        print(f"  {stage:<12} {count:>4}")
    print()
    for row in turnout:
        # `.astimezone()` with no argument converts to the host's timezone, which is
        # what the screen shows. This printed `row.starts_at.date()` — the UTC date —
        # so an evening event west of UTC was reported here on one day and rendered in
        # the app on another, from the same store. Both are display
        # surfaces for the same person; the arithmetic underneath stays UTC.
        local_date = row.starts_at.astimezone().date()
        # `attended` on these rows counts registrations, so the row says so at the number.
        mark = "  registration only" if row.show_rate is None and row.attendance_evidence == REGISTRATION_ONLY else ""
        print(
            f"  {local_date}  {_fit(row.title):<{TITLE_WIDTH}} "
            f"{row.attended:>3} attended  {row.new_attendees:>3} new  {row.no_shows:>3} no-show{mark}"
        )
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from baraza.mcp.server import serve_stdio

    serve_stdio(args.store)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
