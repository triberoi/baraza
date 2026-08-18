"""This package's own engagement score and lifecycle labels.

The score averages commitment, reliability and recency, each 0-1, equally weighted —
unequal weights would make this a tuning table. A part with no denominator is dropped
rather than counted as zero; with nothing settled yet the score is ``None``, never 0.

A member only lapses if an event happened during the window they missed, measured from
when THEY were last seen. A calendar-wide flag un-flags the whole roster during any
quiet stretch.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta

from baraza.analytics.members import AttendanceRecord, EventRecord, MemberFacts, member_facts
from baraza.analytics.thresholds import Thresholds

PROSPECT = "prospect"
FIRST_TIMER = "first_timer"
REGULAR = "regular"
CHAMPION = "champion"
LAPSED = "lapsed"

#: Every label :func:`lifecycle_label` can return, in display order. Ordered because
#: the middle three are a ramp, which the UI draws as one ordinal scale.
LIFECYCLE_LABELS = (PROSPECT, FIRST_TIMER, REGULAR, CHAMPION, LAPSED)


@dataclass(frozen=True)
class ScoredMember:
    """A member's counted facts, plus the two judgments made from them."""

    facts: MemberFacts
    score: int | None
    lifecycle: str

    @property
    def member_id(self) -> str:
        return self.facts.member_id


def recency(
    facts: MemberFacts,
    *,
    thresholds: Thresholds,
    now: datetime,
    last_settled_event_at: datetime | None = None,
) -> float:
    """``1.0`` inside the lapse window, sliding to ``0.0`` at twice it. Measured from the
    last event they ATTENDED, so registering monthly and never coming scores as absent.

    :param last_settled_event_at: the most recent settled event on the calendar. Elapsed
        time is measured to it rather than to ``now``, so a member's recency stops
        decaying once there is nothing left for them to miss — an organizer who runs
        nothing for a season must not drag every member's score down. This is the same
        availability gate :func:`lifecycle_label` applies to the LABEL. ``None`` measures
        to ``now``.
    """
    if facts.last_attended_at is None:
        return 0.0
    window = float(thresholds.lapsed_after_days)
    measured_to = now if last_settled_event_at is None else min(last_settled_event_at, now)
    days = (measured_to - facts.last_attended_at).total_seconds() / 86400.0
    if days <= window:
        # Covers negative `days` (a check-in recorded ahead of its event's start)
        # without needing a guard: this branch always returns the constant.
        return 1.0
    if days >= 2 * window:
        return 0.0
    return (2 * window - days) / window


def score_member(
    facts: MemberFacts,
    *,
    thresholds: Thresholds,
    now: datetime,
    last_settled_event_at: datetime | None = None,
) -> int | None:
    """The 0-100 engagement score, or ``None`` when nothing has settled for this member
    yet. The three parts are in the module docstring. ``last_settled_event_at`` is passed
    through to :func:`recency`, which is where it matters."""
    if facts.opportunities == 0:
        return None

    parts = [
        facts.attendance_rate,
        recency(facts, thresholds=thresholds, now=now, last_settled_event_at=last_settled_event_at),
    ]
    commitments = facts.events_attended + facts.no_shows
    if commitments:
        parts.append(facts.events_attended / commitments)

    return round(100 * sum(parts) / len(parts))


def lifecycle_label(
    facts: MemberFacts,
    *,
    thresholds: Thresholds,
    now: datetime,
    an_event_happened_while_absent: bool,
) -> str:
    """One of :data:`LIFECYCLE_LABELS`.

    :param an_event_happened_while_absent: whether any event started after this member was
        last seen. Per-member, never a calendar-wide flag.
    """
    # The second half is implied by the first and cannot fire alone. It is written out
    # because it narrows the type for the subtraction below; an `assert` would be
    # stripped by `python -O`.
    if facts.events_attended == 0 or facts.last_attended_at is None:
        return PROSPECT

    lapsed = now - facts.last_attended_at > timedelta(days=thresholds.lapsed_after_days)
    if lapsed and an_event_happened_while_absent:
        return LAPSED
    came_often = facts.events_attended >= thresholds.champion_min_events
    came_reliably = facts.attendance_rate >= thresholds.champion_min_rate
    if came_often and came_reliably:
        return CHAMPION
    if facts.events_attended >= thresholds.regular_min_events:
        return REGULAR
    return FIRST_TIMER


def analyze_members(
    events: list[EventRecord],
    attendances: list[AttendanceRecord],
    *,
    now: datetime,
    thresholds: Thresholds | None = None,
) -> list[ScoredMember]:
    """Count, score and label every member, in ``member_id`` order."""
    settings = thresholds or Thresholds()
    # Settled events, sorted once, so "did anything run while this member was away" is a
    # binary search per member. Cut on `over_by`, matching `member_facts`: a running
    # event must not tip a member into `lapsed` while it is still in the room.
    settled = sorted(event.starts_at for event in events if event.over_by <= now)
    # The same availability gate the label uses, in the form the SCORE needs: the label
    # asks whether anything ran while the member was away, the score has to know how long
    # the calendar itself has been quiet. Without it a hiatus decays everyone equally.
    last_settled_event_at = settled[-1] if settled else None

    def an_event_happened_since(last_attended_at: datetime | None) -> bool:
        if last_attended_at is None:
            return bool(settled)
        return bisect_right(settled, last_attended_at) < len(settled)

    return [
        ScoredMember(
            facts=facts,
            score=score_member(
                facts, thresholds=settings, now=now, last_settled_event_at=last_settled_event_at
            ),
            lifecycle=lifecycle_label(
                facts,
                thresholds=settings,
                now=now,
                an_event_happened_while_absent=an_event_happened_since(facts.last_attended_at),
            ),
        )
        for facts in member_facts(events, attendances, now=now)
    ]


__all__ = [
    "CHAMPION",
    "FIRST_TIMER",
    "LAPSED",
    "LIFECYCLE_LABELS",
    "PROSPECT",
    "REGULAR",
    "ScoredMember",
    "analyze_members",
    "lifecycle_label",
    "recency",
    "score_member",
]
