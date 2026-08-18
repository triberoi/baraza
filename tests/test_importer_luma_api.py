"""Tests for the paid path (``baraza.importer.import_luma_api``).

The client is a fake throughout — it is someone else's service, and a suite that
reached it would be testing Luma's uptime and would fail on a train.

The theme here is **the two paths must not disagree**. A free organizer and a paying
one look at the same community; if the derived no-show, the member id or the status
vocabulary differed between them, the tool would be telling two people two different
stories about the same event. So most of what is checked is sameness, and the
divergences that are real are checked for being exactly the ones that are written down.
"""

from datetime import UTC, datetime
from pathlib import Path

from baraza.importer import import_folder, import_luma_api
from baraza.ingest import LumaEvent, LumaGuest, email_to_member_id
from baraza.store import StoredEvent, connect

D = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
NOW = D(2026, 6, 1)


class FakeLuma:
    """A calendar that answers like Luma's API does, and counts what was asked of it."""

    def __init__(self, events: list[LumaEvent], guests: dict[str, list[LumaGuest]]) -> None:
        self._events = events
        self._guests = guests
        self.probes = 0

    def probe(self) -> None:
        self.probes += 1

    def list_events(self) -> list[LumaEvent]:
        return list(self._events)

    def get_event(self, api_id: str) -> LumaEvent | None:
        return next((e for e in self._events if e.api_id == api_id), None)

    def list_event_guests(self, event_api_id: str) -> list[LumaGuest]:
        return list(self._guests.get(event_api_id, []))


def _event(api_id: str, name: str, start: datetime, end: datetime | None = None) -> LumaEvent:
    return LumaEvent(
        api_id=api_id,
        name=name,
        start_at=start,
        end_at=end,
        capacity=50,
        venue="The Hall",
        created_at=None,
        tags=("python",),
    )


def _guest(email: str, status: str, *, name: str | None = None, checked_in: datetime | None = None) -> LumaGuest:
    return LumaGuest(
        api_id=f"g-{email}",
        approval_status=status,
        user_email=email,
        user_name=name,
        registered_at=D(2026, 1, 1),
        checked_in_at=checked_in,
    )


CALENDAR = FakeLuma(
    events=[
        _event("evt-jan", "January", D(2026, 1, 10), D(2026, 1, 10, 20)),
        _event("evt-feb", "February", D(2026, 2, 10), D(2026, 2, 10, 20)),
    ],
    guests={
        "evt-jan": [
            _guest("amy@x.com", "approved", name="Amy", checked_in=D(2026, 1, 10, 18)),
            _guest("bo@x.com", "approved", name="Bo"),
        ],
        "evt-feb": [_guest("amy@x.com", "approved", name="Amy", checked_in=D(2026, 2, 10, 18))],
    },
)


def test_the_whole_calendar_lands_with_the_fields_only_the_api_has(tmp_path: Path) -> None:
    """Venue and tags are their own screens and the CSV path cannot supply them — which
    is most of what a paying organizer is paying for here."""
    store = connect(tmp_path / "b.db", create=True)
    result = import_luma_api(store, CALENDAR, now=NOW)

    assert (result.events, result.members, result.attendances) == (2, 2, 3)
    assert result.pending == (), "the API answers with names and dates, so nothing can be pending"
    january = store.events()[0]
    assert (january.title, january.venue, january.tags) == ("January", "The Hall", ("python",))
    assert january.ends_at == D(2026, 1, 10, 20)
    store.close()


def test_the_no_show_is_derived_the_same_way_as_on_the_free_path(tmp_path: Path) -> None:
    """Bo said yes and never checked in. Both paths must call that a no-show, or the
    tool tells a paying organizer and a free one different things about one guest."""
    store = connect(tmp_path / "b.db", create=True)
    import_luma_api(store, CALENDAR, now=NOW)
    assert {a.member_id: a.status for a in store.attendances()}[email_to_member_id("bo@x.com")] == "no_show"
    store.close()


def test_the_member_id_is_the_same_id_the_free_path_produces(tmp_path: Path) -> None:
    """The join key. An organizer who starts on CSVs and later buys a key must not end
    up with two of every person."""
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "a.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Amy,amy@x.com,2026-01-01T00:00:00Z,approved,2026-01-10T18:00:00Z,https://lu.ma/check-in/evt-jan\n",
        encoding="utf-8",
    )
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events([StoredEvent("evt-jan", "January", D(2026, 1, 10))])
    import_folder(store, folder, now=NOW, resolve=None)
    from_csv = {m.member_id for m in store.members()}

    import_luma_api(store, CALENDAR, now=NOW)
    assert from_csv <= {m.member_id for m in store.members()}
    assert len(store.members()) == 2, "Amy arrived twice and is one member"
    store.close()


def test_luma_wins_over_a_hand_typed_title(tmp_path: Path) -> None:
    """The deliberate opposite of the folder path, and for the same underlying reason:
    prefer the better source. A title typed because a lookup failed loses to the
    calendar itself answering."""
    store = connect(tmp_path / "b.db", create=True)
    store.upsert_events([StoredEvent("evt-jan", "What the organizer guessed", D(2026, 3, 1))])
    import_luma_api(store, CALENDAR, now=NOW)

    assert store.events()[0].title == "January"
    store.close()


def test_a_guest_with_no_email_is_reported_rather_than_dropped_in_silence(tmp_path: Path) -> None:
    """Same rule as the free path: no email, no member id, no join across imports — so
    it has to be visible that somebody was left out."""
    calendar = FakeLuma(
        events=[_event("evt-jan", "January", D(2026, 1, 10))],
        guests={"evt-jan": [_guest("", "approved", name="Anon"), _guest("amy@x.com", "approved")]},
    )
    store = connect(tmp_path / "b.db", create=True)
    result = import_luma_api(store, calendar, now=NOW)

    assert result.members == 1
    assert any("no email address" in w for w in result.warnings)
    store.close()


def test_importing_the_same_calendar_twice_changes_nothing(tmp_path: Path) -> None:
    """The refresh button on the paid path."""
    store = connect(tmp_path / "b.db", create=True)
    import_luma_api(store, CALENDAR, now=NOW)
    snapshot = (store.events(), store.members(), store.attendances())
    import_luma_api(store, CALENDAR, now=NOW)
    assert (store.events(), store.members(), store.attendances()) == snapshot
    store.close()


def test_an_empty_calendar_is_not_an_error(tmp_path: Path) -> None:
    """A brand-new Luma account with nothing on it. The first-run screen calls this."""
    store = connect(tmp_path / "b.db", create=True)
    result = import_luma_api(store, FakeLuma(events=[], guests={}), now=NOW)
    assert (result.events, result.members, result.attendances) == (0, 0, 0)
    store.close()


def test_the_result_is_stable_whatever_order_luma_answers_in(tmp_path: Path) -> None:
    """Luma's pagination order is not ours to rely on, and the tool promises the same
    data to produce the same result."""
    reversed_calendar = FakeLuma(events=list(reversed(CALENDAR.list_events())), guests=CALENDAR._guests)  # noqa: SLF001
    first, second = connect(tmp_path / "a.db", create=True), connect(tmp_path / "b.db", create=True)
    import_luma_api(first, CALENDAR, now=NOW)
    import_luma_api(second, reversed_calendar, now=NOW)

    assert (first.events(), first.members(), first.attendances()) == (
        second.events(),
        second.members(),
        second.attendances(),
    )
    first.close()
    second.close()


def test_every_guest_with_a_blank_email_is_not_the_same_fictional_person(tmp_path: Path) -> None:
    """``if not guest.user_email`` let a single
    space through, because a space is truthy; ``email_to_member_id`` then stripped it
    to the empty string and hashed that. Every such guest, across every event, landed
    on one member id — one fictional person who was then scored, labelled and counted
    like anyone real. The free route escaped it only because the CSV parser happens to
    trim at parse time, which is luck rather than a guard.

    Reported too, which it also was not: the free route warned and this one said
    nothing at all.
    """
    calendar = FakeLuma(
        events=[_event("evt-1", "May", D(2026, 5, 1), D(2026, 5, 1, 2))],
        guests={
            "evt-1": [
                _guest("ada@x.com", "approved", name="Ada"),
                _guest(" ", "approved", name="Nobody"),
                _guest("\t", "approved", name="Nobody Else"),
                _guest("", "approved", name="Nor Anyone"),
            ]
        },
    )
    store = connect(tmp_path / "b.db", create=True)
    result = import_luma_api(store, calendar, now=NOW)
    members = store.members()
    store.close()

    assert [m.email for m in members] == ["ada@x.com"]
    assert email_to_member_id("") not in {m.member_id for m in members}
    assert sum("no email address" in w for w in result.warnings) == 3


def test_a_person_keeps_one_name_and_one_email_across_both_routes(tmp_path: Path) -> None:
    """The same person imported both ways used to come back different: the
    free route lowercased and trimmed the address and derived a name from it, this one
    stored whatever Luma sent — including an empty name — and neither refused to
    overwrite the better value. So the organizer watched a member's details flip back
    and forth depending on which route ran last.

    Both halves are needed and both are asserted here: the two routes now NORMALIZE the
    same way, so they agree in the first place, and the store refuses to let a blank
    replace a value, so a disagreement could not destroy the better answer anyway.
    """
    store = connect(tmp_path / "b.db", create=True)
    # The API answers with an untidy address and no name at all.
    calendar = FakeLuma(
        events=[_event("evt-1", "May", D(2026, 5, 1), D(2026, 5, 1, 2))],
        guests={"evt-1": [_guest("  Ada@X.COM ", "approved", name=None)]},
    )
    import_luma_api(store, calendar, now=NOW)
    after_api = store.members()

    # ...then the same person arrives through the free route, tidily. The event is
    # seeded so the folder route finds it in the store and needs no resolver at all.
    store.upsert_events([StoredEvent("evt-2", "June", D(2026, 6, 1), D(2026, 6, 1, 2))])
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "a.csv").write_text(
        "guest_id,name,email,created_at,approval_status,checked_in_at,qr_code_url\n"
        "g1,Ada Lovelace,ada@x.com,2026-05-01T09:00:00Z,approved,,https://lu.ma/check-in/evt-2\n",
        encoding="utf-8",
    )
    import_folder(store, folder, now=NOW, resolve=None)
    after_folder = store.members()
    store.close()

    # One person, not two: the untidy address normalized to the same member id.
    assert len(after_api) == 1 and len(after_folder) == 1
    assert after_api[0].member_id == after_folder[0].member_id == email_to_member_id("ada@x.com")
    assert after_api[0].email == "ada@x.com"  # stored tidy, not as Luma sent it
    # Luma named nobody, so the store says so rather than inventing one — and both routes
    # say it the same way, which is the agreement this test exists for. The stand-in a
    # screen needs is derived; persisting it is what let a blank export
    # overwrite a real name.
    assert after_api[0].name == ""
    assert after_api[0].display_name == "ada"
    # The real name from the second route wins; nothing flipped back to a blank.
    assert after_folder[0].name == "Ada Lovelace"
    assert after_folder[0].display_name == "Ada Lovelace"
