"""What an MCP client can ask about a calendar, as plain functions.

No ``mcp`` import here, deliberately: the answers are testable without standing up a
protocol, and the dependency is an opt-in extra, so only the registration shim needs it.

SECURITY: read-only. There is no import tool and no settings tool — a tool that can write
is one a model will eventually write with.

Every answer comes from the same functions the screens use, so a model and an organizer
cannot end up describing different communities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from baraza.analytics import analyze_members
from baraza.analytics.retention import Cohort, retention_grid
from baraza.analytics.views import event_attendance, lifecycle_mix, member_timeline
from baraza.store import Store, StoredMember, connect

#: A roster is unbounded and a model's context is not. Past a few hundred rows the useful
#: question is an aggregate one, and a stated truncation beats a reply too large to return.
MAX_PEOPLE = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _opened(store_path: Path | str) -> Store:
    """Read-only, and it means it: no schema convergence, and no store created at a path
    typo — an invented empty store answers "your community is empty"."""
    return connect(store_path, read_only=True)


def _disclosable_name(member: StoredMember | None) -> str | None:
    """The name the MCP surface may hand an assistant: the real stored name, or ``None``.

    Never :meth:`StoredMember.display_name` — that falls back to the email local part, and
    the README promises names, never email addresses. A nameless member is ``null`` here;
    ``member_id`` already identifies them for a follow-up question.
    """
    return (member.name.strip() or None) if member is not None else None


def overview(store_path: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """The headline: how many events, how many people, turnout, and the lifecycle mix."""
    moment = now or _now()
    store = _opened(store_path)
    try:
        events, attendances = store.calendar()
        thresholds = store.thresholds()
    finally:
        store.close()

    members = analyze_members(events, attendances, now=moment, thresholds=thresholds)
    turnout = event_attendance(events, attendances, now=moment)
    attended = sum(row.attended for row in turnout)
    committed = sum(row.committed for row in turnout)
    return {
        "events_held": len(turnout),
        "people": len(members),
        "attendances": attended,
        # None, not 0, when nobody has said yes yet. A model repeats whichever it gets.
        "show_rate": round(attended / committed, 3) if committed else None,
        "lifecycle_mix": lifecycle_mix(members),
        "events": [
            {
                "event_id": row.event_id,
                "title": row.title,
                "starts_at": row.starts_at.isoformat(),
                "attended": row.attended,
                "no_shows": row.no_shows,
                "new_attendees": row.new_attendees,
                "returning_attendees": row.returning_attendees,
            }
            for row in turnout
        ],
    }


def people(store_path: Path | str, *, limit: int = 50, now: datetime | None = None) -> dict[str, Any]:
    """The roster, highest score first, truncated to ``limit``.

    The truncation is reported, never silent — a quietly halved community gets repeated to
    the organizer as though it were all of it. ``limit=0`` returns no people: a floor of 1
    would override the one limit a caller states most deliberately.
    """
    moment = now or _now()
    store = _opened(store_path)
    try:
        events, attendances = store.calendar()
        thresholds = store.thresholds()
        known = {m.member_id: m for m in store.members()}
    finally:
        store.close()

    scored = analyze_members(events, attendances, now=moment, thresholds=thresholds)
    ranked = sorted(scored, key=lambda m: (-(m.score if m.score is not None else -1), m.member_id))
    # Clamped to [0, MAX_PEOPLE]: zero is a real answer, negative is not a request.
    capped = max(0, min(limit, MAX_PEOPLE))
    return {
        "total": len(ranked),
        "returned": min(capped, len(ranked)),
        "truncated": len(ranked) > capped,
        "people": [
            {
                "member_id": m.member_id,
                "name": _disclosable_name(known.get(m.member_id)),
                "score": m.score,
                "lifecycle": m.lifecycle,
                "events_attended": m.facts.events_attended,
                "no_shows": m.facts.no_shows,
                "attendance_rate": round(m.facts.attendance_rate, 3),
            }
            for m in ranked[:capped]
        ],
    }


def person(store_path: Path | str, member_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One member, and every event since they first appeared, the ignored ones included."""
    moment = now or _now()
    store = _opened(store_path)
    try:
        events, attendances = store.calendar()
        thresholds = store.thresholds()
        known = {m.member_id: m for m in store.members()}
    finally:
        store.close()

    scored = {m.member_id: m for m in analyze_members(events, attendances, now=moment, thresholds=thresholds)}
    if member_id not in scored:
        raise LookupError(f"no member {member_id!r} in this store")

    member = scored[member_id]
    return {
        "member_id": member_id,
        "name": _disclosable_name(known.get(member_id)),
        "score": member.score,
        "lifecycle": member.lifecycle,
        "events_attended": member.facts.events_attended,
        "no_shows": member.facts.no_shows,
        "timeline": [
            {
                "title": entry.title,
                "starts_at": entry.starts_at.isoformat(),
                # `null` means they have no record for this event. For a PAST event that
                # is the interesting case — it happened in their era and they did not
                # engage with it at all, which is not the same as cancelling. For a
                # future one it means only that it has not happened yet: the timeline
                # deliberately includes those, and `starts_at` is what tells the two
                # apart. This comment used to describe only the first, which is the same
                # wrong reading an earlier review found on the person screen.
                "status": entry.status,
            }
            for entry in member_timeline(member_id, events, attendances)
        ],
    }


def retention(store_path: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """Repeat attendance by cohort.

    ``months`` minus ``active_months`` is the set of cells that mean *no event
    happened*, as opposed to *nobody came*. A model handed a grid without that
    distinction will confidently report a healthy community as collapsing.

    ``in_progress_month`` is the third reason a cell is null and ships for the same
    reason: the current month has not finished being counted, so
    its cells are blank in a way that is neither "no event" nor "beyond the grid".
    This docstring used to say the first split was *exactly* the null set, and once
    the in-progress month became blank that sentence was the confident wrong report
    it warns about.

    **Each cohort's returns name their own month**. They used to be
    a bare list, and it was misreadable three ways at once, each of which turns a
    healthy community into a collapsing one in a summary nobody double-checks:

    * it was offset from the **cohort's** first month while ``months`` is the grid's
      axis, so lining the two up — the obvious thing to do — shifted every later cohort;
    * its first entry was always the cohort's SIZE, not a return, so every row appeared
      to start at 100% and fall;
    * and it was called ``returned``, which invites reading entry 0 as a return.

    A list of ``{month, returned, rate}`` cannot be misaligned, and offset 0 is simply
    not in it — ``size`` already says what it said.
    """
    moment = now or _now()
    store = _opened(store_path)
    try:
        events, attendances = store.calendar()
    finally:
        store.close()

    grid = retention_grid(events, attendances, now=moment)
    return {
        "months": list(grid.months),
        "active_months": sorted(grid.active_months),
        "in_progress_month": grid.in_progress_month,
        "note": (
            "A null `returned` means the question could not be asked — no event ran that month, "
            "the month has not arrived, or it is `in_progress_month` and has not finished. "
            "It never means nobody came back. Each cohort's `returns` name their own months, "
            "and start the month AFTER the cohort formed: `size` is how many first attended."
        ),
        "cohorts": [
            {
                "month": cohort.month,
                "size": cohort.size,
                "returns": _returns(grid.months, cohort),
            }
            for cohort in grid.cohorts
        ],
    }


def _returns(axis: tuple[str, ...], cohort: Cohort) -> list[dict[str, Any]]:
    """One self-describing entry per month AFTER a cohort formed. Offset 0 is skipped: it
    is the cohort's size by definition, and reporting it as a return makes every row look
    like a fall from 100%."""
    start = axis.index(cohort.month)
    return [
        {
            "month": axis[start + offset],
            "returned": count,
            "rate": None if count is None else round(count / cohort.size, 3),
        }
        for offset, count in enumerate(cohort.attended)
        if offset > 0
    ]


#: Every tool the MCP server exposes, and the whole of what it can do. Named here so
#::mod:`baraza.mcp.server` registers a list rather than a memory, and a test can assert
#: the surface without importing the protocol.
TOOLS = (overview, people, person, retention)

__all__ = ["MAX_PEOPLE", "TOOLS", "overview", "people", "person", "retention"]
