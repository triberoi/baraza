"""Per-member counts, the engagement score, lifecycle labels, and the roll-ups behind
the screens.

Nothing here reads a network, a database or a clock. ``now`` is always an argument.
"""

from __future__ import annotations

from baraza.analytics.members import (
    AttendanceRecord,
    EventRecord,
    MemberFacts,
    member_facts,
    validate_calendar,
)
from baraza.analytics.retention import (
    Cohort,
    RetentionGrid,
    retention_grid,
)
from baraza.analytics.scoring import (
    CHAMPION,
    FIRST_TIMER,
    LAPSED,
    LIFECYCLE_LABELS,
    PROSPECT,
    REGULAR,
    ScoredMember,
    analyze_members,
    lifecycle_label,
    recency,
    score_member,
)
from baraza.analytics.thresholds import Thresholds
from baraza.analytics.views import (
    EventAttendance,
    TimelineEntry,
    event_attendance,
    lifecycle_mix,
    member_timeline,
)

__all__ = [
    "CHAMPION",
    "FIRST_TIMER",
    "LAPSED",
    "LIFECYCLE_LABELS",
    "PROSPECT",
    "REGULAR",
    "AttendanceRecord",
    "Cohort",
    "EventAttendance",
    "EventRecord",
    "MemberFacts",
    "RetentionGrid",
    "ScoredMember",
    "Thresholds",
    "TimelineEntry",
    "analyze_members",
    "event_attendance",
    "lifecycle_label",
    "lifecycle_mix",
    "member_facts",
    "member_timeline",
    "recency",
    "retention_grid",
    "score_member",
    "validate_calendar",
]
