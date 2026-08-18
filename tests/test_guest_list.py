"""Tests for the Luma guest-list CSV parser and combiner (``baraza.ingest.guest_list``).

Pure: a string goes in, structured guests come out. No network, no database, no clock —
which is the point of the module and the reason these tests moved here with it.

These cover the parser and the combiner. Row assembly and the write path belong to
whatever consumes them and are tested there.
"""

from datetime import UTC, datetime

import pytest

from baraza.ingest.guest_list import (
    FEEDBACK_MAX_CHARS,
    LumaParseError,
    ParsedLumaFile,
    combine_luma_guest_lists,
    infer_title_from_filename,
    parse_luma_guest_list,
)

QR = "https://luma.com/check-in/evt-ABC123?pk=k"

GUESTS_CSV = (
    "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
    f"g1,Alice,alice@x.com,2026-04-20T00:00:00Z,approved,2026-05-01T18:05:00Z,{QR}1\n"
    f"g2,,bob@x.com,2026-04-21T00:00:00Z,approved,,{QR}2\n"
    f"g3,Carol,carol@x.com,2026-04-22T00:00:00Z,waitlist,,{QR}3\n"
)


# --- parser ---------------------------------------------------------------------
def test_parse_extracts_evt_id_and_guests() -> None:
    gl = parse_luma_guest_list(GUESTS_CSV)
    assert gl.luma_event_id == "evt-ABC123"
    assert [g.luma_guest_id for g in gl.guests] == ["g1", "g2", "g3"]
    g2 = gl.guests[1]
    # The parser reports what the export said, and this row said nothing. The email
    # local part used to be substituted here, which put a guess in the store where a
    # fact belongs and let a later blank export overwrite a real name.
    # The stand-in now lives on `StoredMember.display_name`.
    assert g2.name == "" and g2.email == "bob@x.com"
    assert gl.guests[0].checked_in_at == datetime(2026, 5, 1, 18, 5, tzinfo=UTC)


def test_parse_raises_when_not_a_luma_export() -> None:
    with pytest.raises(LumaParseError):
        parse_luma_guest_list("guest_id,name,email\ng1,A,a@x\n")  # no qr_code_url evt- id


def test_parse_warns_on_row_anomalies() -> None:
    csv = (
        "guest_id,name,email,created_at,approval_status,qr_code_url\n"
        f",X,x@x,2026-04-20T00:00:00Z,approved,{QR}1\n"  # missing guest_id → skip
        f"g9,Y,y@x,not-a-date,approved,{QR}2\n"  # bad created_at → skip
        f"g8,Z,z@x,2026-04-20T00:00:00Z,mystery,{QR}3\n"  # unrecognized approval → warned, kept
    )
    gl = parse_luma_guest_list(csv)
    assert [g.luma_guest_id for g in gl.guests] == ["g8"]
    assert gl.guests[0].luma_approval_status == "mystery"
    assert any("missing guest_id" in w for w in gl.warnings)
    assert any("not a parseable date" in w for w in gl.warnings)
    assert any("not one we recognize" in w for w in gl.warnings)


def test_parse_captures_survey_rating_and_feedback() -> None:
    """The rating (incl. Luma's float-like '4.0' rendering) + feedback are captured;
    blanks stay None."""
    csv = (
        "guest_id,name,email,created_at,approval_status,qr_code_url,survey_response_rating,survey_response_feedback\n"
        f"g1,A,a@x,2026-04-20T00:00:00Z,approved,{QR}1,5,Loved it\n"
        f"g2,B,b@x,2026-04-20T00:00:00Z,approved,{QR}2,4.0,\n"
        f"g3,C,c@x,2026-04-20T00:00:00Z,approved,{QR}3,,\n"
    )
    gl = parse_luma_guest_list(csv)
    assert (gl.guests[0].survey_response_rating, gl.guests[0].survey_response_feedback) == (5, "Loved it")
    assert (gl.guests[1].survey_response_rating, gl.guests[1].survey_response_feedback) == (4, None)
    assert (gl.guests[2].survey_response_rating, gl.guests[2].survey_response_feedback) == (None, None)
    assert gl.warnings == []


def test_parse_drops_bad_ratings_with_a_warning_never_the_row() -> None:
    csv = (
        "guest_id,name,email,created_at,approval_status,qr_code_url,survey_response_rating\n"
        f"g1,A,a@x,2026-04-20T00:00:00Z,approved,{QR}1,six\n"  # not a number
        f"g2,B,b@x,2026-04-20T00:00:00Z,approved,{QR}2,7\n"  # out of range
        f"g3,C,c@x,2026-04-20T00:00:00Z,approved,{QR}3,3.5\n"  # not whole
    )
    gl = parse_luma_guest_list(csv)
    assert [g.luma_guest_id for g in gl.guests] == ["g1", "g2", "g3"]  # rows all kept
    assert all(g.survey_response_rating is None for g in gl.guests)
    assert sum("rating dropped" in w for w in gl.warnings) == 3


def test_parse_truncates_oversize_feedback() -> None:
    long_text = "x" * (FEEDBACK_MAX_CHARS + 50)
    csv = (
        "guest_id,name,email,created_at,approval_status,qr_code_url,survey_response_feedback\n"
        f"g1,A,a@x,2026-04-20T00:00:00Z,approved,{QR}1,{long_text}\n"
    )
    gl = parse_luma_guest_list(csv)
    fb = gl.guests[0].survey_response_feedback
    assert fb is not None and len(fb) == FEEDBACK_MAX_CHARS
    assert any("truncated" in w for w in gl.warnings)


def test_infer_title_from_filename() -> None:
    assert infer_title_from_filename("AI_Meetup_-_Guests_-_2026-05-18-14-30-15.csv") == "AI Meetup"
    assert infer_title_from_filename("random.csv") is None


def test_infer_title_from_a_real_luma_download_name() -> None:
    """Luma names the download with spaces, not underscores. The pattern knew only the
    underscore form, so every title in an unrenamed real download came back as None and
    the organizer retyped names the filenames already carried — found by importing four
    real exports on 2026-08-16, all four "(no title in the filename)"."""
    assert (
        infer_title_from_filename("How Experts Actually Use AI - Guests - 2026-05-18-22-00-10.csv")
        == "How Experts Actually Use AI"
    )
    # A mixed form (a sync client sanitizing part of the name) still parses.
    assert infer_title_from_filename("AI Night_- Guests -_2026-05-18-22-00-10.csv") == "AI Night"
    # A hyphenated TITLE with spaced separators must not lose its tail to the split.
    assert (
        infer_title_from_filename("Founders - and Friends - Guests - 2026-05-18-22-00-10.csv")
        == "Founders - and Friends"
    )


# --- combiner -------------------------------------------------------------------
def test_combine_groups_and_dedupes_latest_rsvp_wins() -> None:
    """The subject is g3 (Carol), who has no check-in in either file. It used to be g1,
    who is checked in in ``GUESTS_CSV`` and not in the re-export — which under the
    tie-break that matters is the *check-in* case, not the later-RSVP one, and asserting
    the re-export won there is asserting the attendance loss."""
    early = parse_luma_guest_list(GUESTS_CSV)
    # a re-export of the same event where g3 re-registered later.
    later_csv = (
        "guest_id,name,email,created_at,approval_status,qr_code_url\n"
        f"g3,Carol,carol@x.com,2026-04-25T00:00:00Z,waitlist,{QR}3\n"
    )
    later = parse_luma_guest_list(later_csv)
    result = combine_luma_guest_lists([ParsedLumaFile("a.csv", early), ParsedLumaFile("b.csv", later)])

    assert len(result.events) == 1  # same evt-id collapses
    ev = result.events[0]
    assert ev.sources == ["a.csv", "b.csv"]
    g3 = next(g for g in ev.guests if g.luma_guest_id == "g3")
    assert g3.rsvp_at == datetime(2026, 4, 25, tzinfo=UTC)  # latest rsvp won
    # ...and g1's check-in from the first file is untouched by the re-export.
    g1 = next(g for g in ev.guests if g.luma_guest_id == "g1")
    assert g1.checked_in_at == datetime(2026, 5, 1, 18, 5, tzinfo=UTC)
    assert result.deduplicated_guest_count == 3 and result.unique_member_count == 3


def test_combine_separates_distinct_events() -> None:
    a = parse_luma_guest_list(GUESTS_CSV)
    other = parse_luma_guest_list(
        "guest_id,name,email,created_at,approval_status,qr_code_url\n"
        "g5,M,m@x,2026-04-20T00:00:00Z,approved,https://luma.com/check-in/evt-ZZZ?pk=p\n"
    )
    result = combine_luma_guest_lists([ParsedLumaFile("a.csv", a), ParsedLumaFile("z.csv", other)])
    assert {e.luma_event_id for e in result.events} == {"evt-ABC123", "evt-ZZZ"}


# --- the export-before / export-after pair ---------------------------
def _guest_csv(guest_id: str, *, rsvp: str, checked_in: str = "") -> str:
    return (
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        f"{guest_id},Alice,alice@x.com,{rsvp},approved,{checked_in},{QR}1\n"
    )


def test_the_post_event_export_check_in_survives_the_pre_event_export() -> None:
    """

    Export the guest list before the event and again after it — the normal loop —
    and the two rows carry the SAME ``created_at``, because that is when the guest
    registered and re-exporting does not change it. The old rule was
    ``guest.rsvp_at > existing.rsvp_at``, which is False on equal timestamps, so
    whichever file was read first won. ``read_folder`` feeds
    ``sorted(directory.glob("*.csv"))``, and Luma's
    ``Name_-_Guests_-_<timestamp>.csv`` convention sorts the pre-event export first.
    The check-in was discarded and the attendee was recorded as a no-show.
    """
    pre = parse_luma_guest_list(_guest_csv("g1", rsvp="2026-04-20T00:00:00Z"))
    post = parse_luma_guest_list(
        _guest_csv("g1", rsvp="2026-04-20T00:00:00Z", checked_in="2026-05-01T18:05:00Z")
    )
    # Alphabetical order, exactly as read_folder supplies it.
    result = combine_luma_guest_lists(
        [
            ParsedLumaFile("Meetup_-_Guests_-_2026-04-30-09-00-00.csv", pre),
            ParsedLumaFile("Meetup_-_Guests_-_2026-05-02-09-00-00.csv", post),
        ]
    )
    (guest,) = result.events[0].guests
    assert guest.checked_in_at == datetime(2026, 5, 1, 18, 5, tzinfo=UTC)


def test_a_check_in_is_not_undone_by_a_later_export_that_lacks_one() -> None:
    """The tie-break's first rule, in the direction that could have been got backwards.
    Luma does not un-check-in a guest, so a row carrying a check-in is never the staler
    of the two — including when it arrives from the EARLIER file. A plain later-file-wins
    rule would have reintroduced the same lost attendance from the other side."""
    checked = parse_luma_guest_list(
        _guest_csv("g1", rsvp="2026-04-20T00:00:00Z", checked_in="2026-05-01T18:05:00Z")
    )
    not_checked = parse_luma_guest_list(_guest_csv("g1", rsvp="2026-04-20T00:00:00Z"))
    result = combine_luma_guest_lists(
        [ParsedLumaFile("a.csv", checked), ParsedLumaFile("z.csv", not_checked)]
    )
    (guest,) = result.events[0].guests
    assert guest.checked_in_at == datetime(2026, 5, 1, 18, 5, tzinfo=UTC)


def test_importing_the_same_pair_in_either_order_gives_the_same_answer() -> None:
    """The property the tie-break has to have, rather than the two examples above:
    deduplication cannot depend on the order the folder happened to be read in."""
    pre = parse_luma_guest_list(_guest_csv("g1", rsvp="2026-04-20T00:00:00Z"))
    post = parse_luma_guest_list(
        _guest_csv("g1", rsvp="2026-04-20T00:00:00Z", checked_in="2026-05-01T18:05:00Z")
    )
    forward = combine_luma_guest_lists([ParsedLumaFile("a.csv", pre), ParsedLumaFile("b.csv", post)])
    backward = combine_luma_guest_lists([ParsedLumaFile("a.csv", post), ParsedLumaFile("b.csv", pre)])
    assert forward.events[0].guests == backward.events[0].guests


def test_caller_order_never_decides_the_fields_the_tie_break_does_not_examine() -> None:
    """On a full tie (same rsvp, same check-in state, same name) the surviving row
    still carries an approval status and a survey rating — fields the evidence chain never
    looks at. The old combiner processed files in caller order and returned the later one
    wholesale, so the prod upload route (which feeds files unsorted) persisted different
    rows for the same two exports depending on receive order. The combiner now sorts by
    filename, so the newer export (later ``<ts>`` in Luma's name) wins regardless of the
    order the caller supplied the files.
    """

    def _csv(approval: str, rating: int) -> str:
        return (
            "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url,"
            "survey_response_rating\n"
            f"g1,Jane,jane@x.com,2026-06-01T10:00:00Z,{approval},2026-06-15T18:05:00Z,{QR}1,{rating}\n"
        )

    earlier = ParsedLumaFile(
        "Meetup_-_Guests_-_2026-06-15-20-00-00.csv", parse_luma_guest_list(_csv("approved", 5))
    )
    later = ParsedLumaFile(
        "Meetup_-_Guests_-_2026-06-16-09-00-00.csv", parse_luma_guest_list(_csv("cancelled", 4))
    )

    forward = combine_luma_guest_lists([earlier, later]).events[0].guests
    backward = combine_luma_guest_lists([later, earlier]).events[0].guests
    assert forward == backward  # order-independent
    (guest,) = forward
    # The newer export (2026-06-16) wins the tie, deterministically.
    assert (guest.luma_approval_status, guest.survey_response_rating) == ("cancelled", 4)


def test_a_genuine_re_registration_still_takes_the_later_rsvp() -> None:
    """Rule 2 is not lost to rule 1: with neither row checked in, the later RSVP wins,
    which is what the original rule was reaching for and got right."""
    early = parse_luma_guest_list(_guest_csv("g1", rsvp="2026-04-20T00:00:00Z"))
    late = parse_luma_guest_list(_guest_csv("g1", rsvp="2026-04-25T00:00:00Z"))
    result = combine_luma_guest_lists([ParsedLumaFile("b.csv", late), ParsedLumaFile("a.csv", early)])
    (guest,) = result.events[0].guests
    assert guest.rsvp_at == datetime(2026, 4, 25, tzinfo=UTC)


# --- one oversized cell used to cost the whole file ------------------
def test_a_cell_past_the_csv_module_limit_no_longer_costs_the_file() -> None:
    """`csv` enforces a module-global 131_072-character field ceiling and raises BEFORE
    yielding any row, so the guest list never reached the FEEDBACK_MAX_CHARS cap that
    exists to handle exactly this — the file failed whole and the cap was unreachable
. The row is now read and then truncated, which is what the cap is for.
    """
    huge = "x" * 200_000
    assert len(huge) > 131_072, "the fixture has to exceed the module default to be the test"
    csv_text = (
        "guest_id,name,email,created_at,approval_status,qr_code_url,survey_response_feedback\n"
        f"g1,A,a@x,2026-04-20T00:00:00Z,approved,{QR}1,{huge}\n"
    )
    gl = parse_luma_guest_list(csv_text)
    feedback = gl.guests[0].survey_response_feedback
    assert feedback is not None and len(feedback) == FEEDBACK_MAX_CHARS
    assert any("truncated" in w for w in gl.warnings)


def test_the_csv_field_limit_is_left_exactly_as_it_was_found() -> None:
    """The ceiling is module-global state shared with every other `csv` reader in the
    process, so raising it for one parse and leaving it raised would be a side effect on
    unrelated code. Asserted across a raising parse too — the restore is in a `finally`
    precisely so a bad file cannot leak it."""
    import csv as csv_module

    before = csv_module.field_size_limit()
    parse_luma_guest_list(GUESTS_CSV)
    assert csv_module.field_size_limit() == before
    with pytest.raises(LumaParseError):
        parse_luma_guest_list("guest_id,name\ng1,A\n")  # no evt- id
    assert csv_module.field_size_limit() == before


# --- two layers, one warning list ------------------------------------
def test_the_blank_email_warning_does_not_promise_what_the_next_layer_undoes() -> None:
    """The parser used to write "guest still included" and the importer then wrote "was
    not imported" about the same guest into the same list. Both were true
    of their own layer and together they are unreadable. This layer now reports only
    what it did — kept the row, no join key — and leaves the fate to whoever decides
    it."""
    csv_text = (
        "guest_id,name,email,created_at,approval_status,qr_code_url\n"
        f"g1,A,,2026-04-20T00:00:00Z,approved,{QR}1\n"
    )
    gl = parse_luma_guest_list(csv_text)
    (warning,) = gl.warnings
    assert "cannot be joined" in warning
    assert "included" not in warning and "imported" not in warning
    assert gl.guests[0].email == ""  # still parsed — this layer drops nothing


# --- the pre-publish security review ------------------------------------------------
def test_concurrent_parses_do_not_leave_the_csv_limit_raised() -> None:
    """`csv.field_size_limit` is process-GLOBAL, and this package is a library.

    Found in review of the finished code, and introduced by its own
    Without a lock two parses interleave: the second reads the first's
    already-raised value as its "previous", and whichever finishes last restores THAT
    instead of the real default. The ceiling is then permanently raised for everything
    else in the process — including a host application that imported
    `parse_luma_guest_list` and parses CSVs of its own, which never asked for it and
    cannot see why its own limits moved.

    Measured before the fix: four threads, and the process ended at 400171 rather than
    131072.
    """
    import csv as csv_module
    import threading

    header = (
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url,"
        "survey_response_feedback\n"
    )

    def sheet(feedback_chars: int) -> str:
        return header + f"g1,A,a@x,2026-04-20T00:00:00Z,approved,,{QR}1,{'x' * feedback_chars}\n"

    # One needs the ceiling far above the default; the other is happy at the default,
    # and is what restores a stale value if the save/restore is not atomic.
    big, small = sheet(400_000), sheet(10)
    default = csv_module.field_size_limit()
    failures: list[str] = []

    def repeatedly(text: str) -> None:
        for _ in range(30):
            try:
                parse_luma_guest_list(text)
            except Exception as exc:  # noqa: BLE001 — any failure at all is the finding
                failures.append(f"{type(exc).__name__}: {exc}")
                return

    threads = [threading.Thread(target=repeatedly, args=(t,)) for t in (big, small, big, small)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures, f"a concurrent parse failed: {failures[:2]}"
    assert csv_module.field_size_limit() == default, (
        "the global csv field limit was left raised — a host application's own parsing "
        "is now running under a ceiling baraza set and never put back"
    )


# --- the header row is checked once, not re-litigated per row ---


def _export(header: str, *rows: str) -> str:
    return header + "\n" + "\n".join(rows) + "\n"


def test_a_repeated_column_is_reported_rather_than_silently_winning() -> None:
    """``csv.DictReader`` keeps the LAST occurrence of a repeated column name, so a file
    carrying ``email`` twice hands back the second value with nothing said. That value
    becomes the member id and the join key across every export, so a duplicated column
    silently re-identifies a guest. Reaches the main product via ``luma_csv.py``.
    """
    text = _export(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url,email",
        "g1,Alice,alice@x.com,2026-01-01T00:00:00Z,approved,,https://lu.ma/check-in/evt-ABC123,evil@z.com",
    )
    parsed = parse_luma_guest_list(text)
    assert any("email" in w and "more than once" in w for w in parsed.warnings), parsed.warnings


def test_a_renamed_required_column_is_one_refusal_not_a_warning_per_row() -> None:
    """The only file-level check was "does some row's qr_code_url hold an evt- id", so a
    file with that and a renamed ``email`` column parsed 'successfully' and dropped every
    row with a per-row complaint that misdescribed the cause — a 500-guest export emitted
    500 of them. The header is knowable before any row is read; say it once.
    """
    text = _export(
        "guest_id,name,email_address,created_at,approval_status,checked_in_at,qr_code_url",
        "g1,Alice,alice@x.com,2026-01-01T00:00:00Z,approved,,https://lu.ma/check-in/evt-ABC123",
        "g2,Bob,bob@x.com,2026-01-02T00:00:00Z,approved,,https://lu.ma/check-in/evt-ABC123",
    )
    with pytest.raises(LumaParseError) as caught:
        parse_luma_guest_list(text)
    assert "email" in str(caught.value)


def test_a_column_we_do_not_require_may_go_missing_without_costing_the_file() -> None:
    """The refusal above must be scoped to what the parser genuinely cannot work without.
    Luma's exports vary — survey columns come and go — and rejecting a file for an absent
    optional column would turn a cosmetic difference into a lost import."""
    text = _export(
        "guest_id,name,email,created_at,approval_status,qr_code_url",  # no checked_in_at
        "g1,Alice,alice@x.com,2026-01-01T00:00:00Z,approved,https://lu.ma/check-in/evt-ABC123",
    )
    parsed = parse_luma_guest_list(text)
    assert [g.luma_guest_id for g in parsed.guests] == ["g1"]
