"""Per-member counts, the engagement score, lifecycle labels, and the roll-ups behind
the screens.

Nothing here reads a network, a database or a clock. ``now`` is always an argument.
"""

from __future__ import annotations

from baraza.analytics.members import (
    ATTENDANCE_EVIDENCE,
    CHECKED_IN,
    REGISTRATION_ONLY,
    AttendanceRecord,
    EventRecord,
    MemberFacts,
    effective_status,
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
    EventCohort,
    TimelineEntry,
    event_attendance,
    event_cohorts,
    lifecycle_mix,
    member_timeline,
)

__all__ = [
    "ATTENDANCE_EVIDENCE",
    "CHAMPION",
    "CHECKED_IN",
    "FIRST_TIMER",
    "LAPSED",
    "LIFECYCLE_LABELS",
    "PROSPECT",
    "REGISTRATION_ONLY",
    "REGULAR",
    "AttendanceRecord",
    "Cohort",
    "EventAttendance",
    "EventCohort",
    "EventRecord",
    "MemberFacts",
    "RetentionGrid",
    "ScoredMember",
    "Thresholds",
    "TimelineEntry",
    "analyze_members",
    "effective_status",
    "event_attendance",
    "event_cohorts",
    "lifecycle_label",
    "lifecycle_mix",
    "member_facts",
    "member_timeline",
    "recency",
    "retention_grid",
    "score_member",
    "validate_calendar",
]
