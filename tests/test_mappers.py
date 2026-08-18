"""Tests for the two shared Luma mappings (``baraza.ingest.mappers``).

Pure functions of their arguments — no clock, no network, no database. ``now`` is
passed in precisely so a status is reproducible, and these tests rely on that.

These cover the two shared mappings. Row assembly is the consumer's schema and is
tested wherever that lives.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baraza.ingest.client import LumaGuest
from baraza.ingest.mappers import (
    GUEST_STATUSES,
    KNOWN_APPROVAL_STATUSES,
    email_to_member_id,
    resolve_guest_status,
)

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
REF = D(2026, 6, 1)

#: The one copy of the member-id vectors, read by this suite AND by
#: `web/tests/data/member_id_vectors.test.ts`. Two files would drift, which is the
#: only class that has ever diverged.
VECTORS_PATH = Path(__file__).parent / "fixtures" / "email_member_id_vectors.json"
MEMBER_ID_VECTORS: list[dict[str, str]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def _guest(status: str, *, checked_in: datetime | None = None) -> LumaGuest:
    return LumaGuest(
        api_id="g",
        approval_status=status,
        user_email="a@x",
        user_name=None,
        registered_at=None,
        checked_in_at=checked_in,
    )


# --- status mapping (incl. the derived no-show) ---------------------------------
@pytest.mark.parametrize(
    ("status", "checked_in", "ended", "expected"),
    [
        ("declined", None, True, "cancelled"),
        ("cancelled", None, True, "cancelled"),
        ("approved", D(2026, 5, 1), True, "attended"),  # check-in wins
        # A check-in outranks a declined/cancelled RSVP. Luma sets `declined` when a
        # guest picks "Not Going", so declining and then turning up is an ordinary
        # sequence, not corrupt data.
        ("declined", D(2026, 5, 1), True, "attended"),
        ("cancelled", D(2026, 5, 1), True, "attended"),
        ("invited", None, True, "invited"),  # never derived to no-show
        ("waitlist", None, True, "waitlisted"),  # never derived to no-show
        ("approved", None, True, "no_show"),  # ended, approved, no check-in
        ("registered", None, True, "no_show"),
        # An applicant the organizer never admitted joins the never-had-a-spot family
        # instead of being derived to a no-show. Asserted before AND after the event
        # ends: the answer must not flip at event end, because nothing about the
        # applicant changed when the event finished.
        ("pending_approval", None, True, "waitlisted"),
        ("pending_approval", None, False, "waitlisted"),
        ("requested", None, True, "waitlisted"),
        ("requested", None, False, "waitlisted"),
        ("pending_approval", D(2026, 5, 1), True, "attended"),  # admitted at the door
        ("approved", None, False, "registered"),  # not ended → still in funnel
    ],
)
def test_resolve_guest_status(status, checked_in, ended, expected) -> None:  # noqa: ANN001
    ended_at = D(2026, 5, 1) if ended else None  # <= REF when ended
    assert resolve_guest_status(_guest(status, checked_in=checked_in), event_ended_at=ended_at, now=REF) == expected


def test_a_status_is_always_one_of_the_published_six() -> None:
    """``GUEST_STATUSES`` is the published vocabulary — a consumer stores these strings,
    so a seventh return value is a breaking change and not a local edit. Asserted as a
    property over the whole input space the function branches on, rather than over the
    examples above: an unknown approval status is the case a table of known ones misses.
    """
    approvals = [
        "approved", "registered",  # the no-show-eligible set
        "pending_approval", "requested", "invited", "waitlist",  # never had a spot
        "declined", "cancelled",
        "", "unknown", "SOMETHING_LUMA_ADDS_LATER",  # what the parser emits, and what it can't know
    ]
    for approval in approvals:
        for checked_in in (None, D(2026, 5, 1)):
            for ended_at in (None, D(2026, 5, 1), D(2026, 12, 1)):  # before REF, after REF
                status = resolve_guest_status(
                    _guest(approval, checked_in=checked_in), event_ended_at=ended_at, now=REF
                )
                assert status in GUEST_STATUSES, f"{approval!r} produced {status!r}, outside the published set"


def test_a_check_in_always_means_attended() -> None:
    """A property over the WHOLE input vocabulary rather than the two rows in the
    table above. A check-in is evidence the person was in the room; every other field is a
    statement of intent recorded beforehand, so no approval status may overrule it. Stated
    this way, a future branch inserted above the check-in test fails here whatever word it
    matches on — which is exactly how the declined case got in.
    """
    for approval in [*KNOWN_APPROVAL_STATUSES, "", "unknown", "SOMETHING_LUMA_ADDS_LATER"]:
        for ended_at in (None, D(2026, 5, 1), D(2026, 12, 1)):
            status = resolve_guest_status(
                _guest(approval, checked_in=D(2026, 5, 1)), event_ended_at=ended_at, now=REF
            )
            assert status == "attended", f"{approval!r} with a check-in produced {status!r}"


def test_nobody_without_a_confirmed_spot_becomes_a_no_show() -> None:
    """A no-show means a place was given and went unused, so the four statuses that
    never carry a place must never derive to one — at any point relative to the event's
    end. Asserted over the set rather than per-status: the bug was one member of the family
    being handled unlike its siblings, which a per-status example cannot see.
    """
    never_had_a_spot = ("invited", "waitlist", "pending_approval", "requested")
    assert set(never_had_a_spot) <= KNOWN_APPROVAL_STATUSES  # the names still exist upstream
    for approval in never_had_a_spot:
        for ended_at in (None, D(2026, 5, 1), D(2026, 12, 1)):
            status = resolve_guest_status(_guest(approval, checked_in=None), event_ended_at=ended_at, now=REF)
            assert status != "no_show", f"{approval!r} derived to a no-show (ended_at={ended_at})"


def test_the_published_vocabulary_is_the_v22_six() -> None:
    """Pins the set itself. Without this the property above holds vacuously the moment
    someone widens ``GUEST_STATUSES`` to admit whatever the function started returning."""
    assert GUEST_STATUSES == {"registered", "attended", "no_show", "cancelled", "waitlisted", "invited"}


def test_routing_cancelled_did_not_add_a_seventh_published_value() -> None:
    """The stated gotcha, asserted rather than assumed. ``GUEST_STATUSES`` is a
    published contract — a consumer persists these strings — so the fix was only safe if
    ``cancelled`` was already IN the set and merely unreachable from the CSV route."""
    assert "cancelled" in GUEST_STATUSES
    assert len(GUEST_STATUSES) == 6


def test_a_word_luma_invents_later_becomes_a_no_show_once_the_event_has_ended() -> None:
    """The reach of a rewritten status word, which is wider than cancellations.

    ``resolve_guest_status`` used to gate its no-show branch on a second allow-list, so
    any word outside it — including the ``'unknown'`` sentinel the parser used to
    manufacture — returned ``registered`` forever. A guest who never came read as
    someone still expected, however long ago the event ended: registered counts
    inflated, no-show counts suppressed, turnout moved with them.

    By that branch the guest is already known not to have cancelled, not to have
    checked in, and to be neither invited nor waitlisted. There is nothing else they
    can be."""
    for approval in ("SOMETHING_LUMA_ADDS_LATER", "unknown", ""):
        ended = resolve_guest_status(_guest(approval), event_ended_at=D(2026, 5, 1), now=REF)
        assert ended == "no_show", f"{approval!r} after the event should be a no-show, got {ended!r}"
        # ...and before the event they are still legitimately expected.
        assert resolve_guest_status(_guest(approval), event_ended_at=D(2026, 12, 1), now=REF) == "registered"


def test_the_input_vocabulary_covers_every_word_the_function_branches_on() -> None:
    """``KNOWN_APPROVAL_STATUSES`` decides whether the parser WARNS. If it listed a word
    the function does not handle, or omitted one it does, the warning would fire on
    exactly the wrong rows — which is how it begins, with the parser holding a
    shorter private list that omitted ``cancelled``."""
    assert KNOWN_APPROVAL_STATUSES == {
        "approved", "registered", "invited", "waitlist", "pending_approval", "requested", "declined", "cancelled",
    }
    # Every recognized word resolves to something other than the catch-all when the
    # event has ended — i.e. the function genuinely knows each of them.
    ended = {resolve_guest_status(_guest(a), event_ended_at=D(2026, 5, 1), now=REF) for a in KNOWN_APPROVAL_STATUSES}
    assert ended == {"cancelled", "invited", "waitlisted", "no_show"}


# --- deterministic member id ----------------------------------------------------
def test_email_to_member_id_is_deterministic_and_case_insensitive() -> None:
    a = email_to_member_id("  Alice@X.COM ")
    assert a == email_to_member_id("alice@x.com")
    assert a.startswith("mem_") and len(a) == 12  # mem_ + 8 hex
    assert email_to_member_id("bob@x.com") != a


def test_email_to_member_id_matches_the_shared_vectors() -> None:
    """The id is stored and joined on, across imports and across channels, so it is a
    frozen value and not an implementation detail: change the hash and every existing
    store silently stops joining.

    Pinned against a fixture that ``web/tests/data/member_id_vectors.test.ts`` reads
    too. This test used to reimplement FNV-1a in Python and compare Python to Python,
    which is why the astral case survived it: the divergence was never between this function
    and the algorithm, it was between this function and the TypeScript copy in
    ``web/lib/data/luma.ts``, and a same-language test cannot see that. The astral
    vectors are the ones that were wrong."""
    for vector in MEMBER_ID_VECTORS:
        assert email_to_member_id(vector["email"]) == vector["memberId"], vector["why"]


def test_the_shared_vectors_cover_the_astral_case_that_diverged() -> None:
    """The fixture is only a guard if it contains the case that broke. An astral
    character (U+10000 and up) is one code point to Python's ``ord`` and a surrogate
    PAIR to JavaScript's ``charCodeAt`` — every other class of character agrees under
    both, so a vector file of ASCII addresses would go green on the bug."""
    astral = [v for v in MEMBER_ID_VECTORS if any(ord(ch) > 0xFFFF for ch in v["email"])]
    assert len(astral) >= 3, "the vectors must keep covering astral characters"
