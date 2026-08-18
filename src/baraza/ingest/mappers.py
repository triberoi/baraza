"""The two pure Luma mappings, shared by the API path and the guest-list CSV.

:data:`GUEST_STATUSES` is a published contract — consumers persist these strings, so
adding a value breaks them.
"""

from __future__ import annotations

from datetime import datetime

from baraza.ingest.client import LumaGuest

#: Every value :func:`resolve_guest_status` can return. Part of the published
#: interface; ``test_mappers.py`` holds the function to it.
GUEST_STATUSES = frozenset({"registered", "attended", "no_show", "cancelled", "waitlisted", "invited"})

#: The INPUT vocabulary, beside the function that branches on it so a second copy
#: cannot drift from it. Not a published contract: it only decides whether to warn about
#: a word Luma has newly invented. An unrecognized word is still resolved by the final
#: branch, never rewritten.
KNOWN_APPROVAL_STATUSES = frozenset(
    {"approved", "registered", "invited", "waitlist", "pending_approval", "requested", "declined", "cancelled"}
)


def email_to_member_id(email: str) -> str:
    """Deterministic member id from an email: FNV-1a 32-bit, lowercased and trimmed so
    casing cannot fracture a member. 32-bit, so collisions are possible but rare."""
    normalized = email.strip().lower()
    h = 0x811C9DC5
    for ch in normalized:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"mem_{h:08x}"


def resolve_guest_status(guest: LumaGuest, *, event_ended_at: datetime | None, now: datetime) -> str:
    """Luma ``approval_status`` → one of :data:`GUEST_STATUSES`.

    The no-show is derived: an approved or registered guest who never checked in by the
    event's end is one. Nobody who was never given a confirmed spot ever is — invited,
    waitlisted, and applicants the organizer never admitted. An unrecognized word falls
    through to the registered family and can become a no-show like any other.
    """
    a = guest.approval_status
    # A check-in is evidence the person was in the room; every other field records an
    # intention stated beforehand, so it is tested first and no approval status overrules
    # it. Luma also sets `declined` when a guest picks "Not Going", so declining and then
    # attending is an ordinary sequence.
    if guest.checked_in_at is not None:
        return "attended"
    if a in ("declined", "cancelled"):
        return "cancelled"
    if a == "invited":
        return "invited"
    # An applicant the organizer never admitted was never given a place, so they join the
    # invited/waitlisted family and are never derived to a no-show: a no-show means a place
    # went unused. `attendances.luma_approval_status` keeps the original word for the
    # application funnel, which this six-value summary cannot carry.
    if a in ("waitlist", "pending_approval", "requested"):
        return "waitlisted"
    if event_ended_at is not None and event_ended_at <= now:
        return "no_show"
    return "registered"


__all__ = [
    "GUEST_STATUSES",
    "KNOWN_APPROVAL_STATUSES",
    "email_to_member_id",
    "resolve_guest_status",
]
