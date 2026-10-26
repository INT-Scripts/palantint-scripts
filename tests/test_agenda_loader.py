from datetime import datetime

import pytest
from sqlalchemy import select

from db.models import Event, EventAttendee, EventOrganization, EventPresenter, ExternalIdentity, Location, Organization, Person
from palantint_scripts.loaders import agenda as agenda_loader
from palantint_scripts.db_helpers import get_or_create_organization

from tests.conftest import write_json


def _agenda_dir(tmp_path):
    d = tmp_path / "agenda"
    d.mkdir(exist_ok=True)
    return d


@pytest.mark.asyncio
async def test_load_agenda_no_index_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(_agenda_dir(tmp_path)))
    await agenda_loader.load_agenda(db_session)
    result = await db_session.execute(select(Event))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_agenda_skips_usr_and_empty_calendars(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    write_json(agenda_dir, "index.json", {
        "USR12345": {"name": "Personal calendar", "event_count": 5},
        "EMPTY1": {"name": "Nothing here", "event_count": 0},
    })
    await agenda_loader.load_agenda(db_session)
    result = await db_session.execute(select(Event))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_agenda_uses_complete_course_scope(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    write_json(agenda_dir, "index.json", {
        "PRJ_CURRENT": {"name": "Current", "event_count": 1, "start_date": "2026-03-01", "end_date": "2026-03-31", "schema_version": 2, "last_sync": "2026-03-01T00:00:00"},
        "PRJ_OLD": {"name": "Old", "event_count": 1},
        "USR_SHARED": {"name": "Shared", "event_count": 1, "last_sync": datetime.now().isoformat()},
    })
    write_json(agenda_dir, "scope.json", {"status": "complete", "calendar_ids": ["PRJ_CURRENT"], "start_date": "2026-03-01", "end_date": "2026-03-31", "last_sync": "2026-03-01T00:00:00"})
    write_json(agenda_dir, "PRJ_CURRENT.json", [await _basic_event("current")])
    write_json(agenda_dir, "PRJ_OLD.json", [await _basic_event("old")])
    write_json(agenda_dir, "USR_SHARED.json", [await _basic_event("shared", groups=["Gp-EI2-G"])])

    await agenda_loader.load_agenda(db_session)
    events = (await db_session.execute(select(Event))).scalars().all()
    assert {event.external_ref for event in events} == {"current"}


async def _basic_event(event_id="evt-1", groups=None, professors=None, room="Amphi A"):
    return {
        "id": event_id,
        "name": "Intro to Algorithms",
        "type": "CM",
        "date": "2026-03-02",
        "start_time": "08h00",
        "end_time": "10h00",
        "room": room,
        "professors": professors or [],
        "groups": groups or [],
    }


@pytest.mark.asyncio
async def test_load_agenda_creates_event_with_room_and_kind(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Gp-EI1 calendar", "event_count": 1}})
    write_json(agenda_dir, "CAL1.json", [await _basic_event()])

    await agenda_loader.load_agenda(db_session)

    result = await db_session.execute(select(Event))
    event = result.scalars().one()
    assert event.external_ref == "evt-1"
    assert event.kind == "COURSE"
    assert event.name == "Intro to Algorithms"

    room = await db_session.get(Location, event.location_id)
    assert room.kind == "ROOM"
    assert room.code == "Amphi A"


@pytest.mark.asyncio
async def test_load_agenda_exam_type_maps_to_exam_kind(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    evt = await _basic_event()
    evt["type"] = "Examen"
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Exams", "event_count": 1}})
    write_json(agenda_dir, "CAL1.json", [evt])

    await agenda_loader.load_agenda(db_session)
    result = await db_session.execute(select(Event))
    assert result.scalars().one().kind == "EXAM"


@pytest.mark.asyncio
async def test_load_agenda_resolves_club_and_class_group_links(db_session, tmp_path, monkeypatch):
    club = await get_or_create_organization(db_session, kind="CLUB", name="Robotics Club")
    class_group = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Gp-EI1")
    await db_session.commit()

    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    evt = await _basic_event(groups=["Robotics Club", "Gp-EI1"])
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Mixed calendar", "event_count": 1}})
    write_json(agenda_dir, "CAL1.json", [evt])

    await agenda_loader.load_agenda(db_session)

    result = await db_session.execute(select(Event))
    event = result.scalars().one()
    assert event.organization_id == club.id

    result = await db_session.execute(
        select(EventOrganization).where(EventOrganization.event_id == event.id)
    )
    links = result.scalars().all()
    assert {l.organization_id for l in links} == {class_group.id}


@pytest.mark.asyncio
async def test_load_agenda_resolves_known_professor_else_falls_back_to_raw(db_session, tmp_path, monkeypatch):
    professor = Person(kind="PROFESSOR", first_name="Ada", last_name="Lovelace")
    db_session.add(professor)
    await db_session.commit()

    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    evt = await _basic_event(event_id="evt-known", professors=["Ada Lovelace"])
    evt2 = await _basic_event(event_id="evt-unknown", professors=["Grace Hopper"])
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Cal", "event_count": 2}})
    write_json(agenda_dir, "CAL1.json", [evt, evt2])

    await agenda_loader.load_agenda(db_session)

    result = await db_session.execute(select(Event).where(Event.external_ref == "evt-known"))
    known_event = result.scalars().one()
    result = await db_session.execute(
        select(EventPresenter).where(EventPresenter.event_id == known_event.id)
    )
    presenters = result.scalars().all()
    assert len(presenters) == 1
    assert presenters[0].person_id == professor.id
    assert known_event.presenters_raw == "Ada Lovelace"

    result = await db_session.execute(select(Event).where(Event.external_ref == "evt-unknown"))
    unknown_event = result.scalars().one()
    result = await db_session.execute(
        select(EventPresenter).where(EventPresenter.event_id == unknown_event.id)
    )
    assert result.scalars().all() == []
    assert unknown_event.presenters_raw == "Grace Hopper"


@pytest.mark.asyncio
async def test_load_agenda_resync_updates_in_place_not_duplicate(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    evt = await _basic_event()
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Cal", "event_count": 1}})
    write_json(agenda_dir, "CAL1.json", [evt])
    await agenda_loader.load_agenda(db_session)

    evt["name"] = "Intro to Algorithms (updated)"
    evt["room"] = "Amphi B"
    write_json(agenda_dir, "CAL1.json", [evt])
    await agenda_loader.load_agenda(db_session)

    result = await db_session.execute(select(Event))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].name == "Intro to Algorithms (updated)"
    room = await db_session.get(Location, events[0].location_id)
    assert room.code == "Amphi B"

    evt["room"] = "salle indiquée sur le planning du Challenge"
    write_json(agenda_dir, "CAL1.json", [evt])
    await agenda_loader.load_agenda(db_session)
    assert events[0].location_id is None


@pytest.mark.asyncio
async def test_load_agenda_unparseable_datetime_is_skipped(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    evt = await _basic_event()
    evt["start_time"] = "not-a-time"
    write_json(agenda_dir, "index.json", {"CAL1": {"name": "Cal", "event_count": 1}})
    write_json(agenda_dir, "CAL1.json", [evt])

    await agenda_loader.load_agenda(db_session)
    result = await db_session.execute(select(Event))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_agenda_links_learners_and_refreshes_attendees(db_session, tmp_path, monkeypatch):
    first = Person(kind="STUDENT", first_name="Florian", last_name="RIVIÈRE")
    second = Person(kind="STUDENT", first_name="Alice", last_name="MARTIN")
    db_session.add_all([first, second])
    await db_session.flush()
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    write_json(agenda_dir, "index.json", {"PRJ1": {"name": "Courses", "event_count": 1}})
    event = await _basic_event(event_id="123", groups=["Unknown course group"])
    event["learners"] = ["RIVIÈRE Florian", "MARTIN Alice"]
    write_json(agenda_dir, "PRJ1.json", [event])
    await agenda_loader.load_agenda(db_session)

    attendees = (await db_session.execute(select(EventAttendee))).scalars().all()
    assert {row.person_id for row in attendees} == {first.id, second.id}

    event["learners"] = ["RIVIÈRE Florian"]
    write_json(agenda_dir, "PRJ1.json", [event])
    await agenda_loader.load_agenda(db_session)
    attendees = (await db_session.execute(select(EventAttendee))).scalars().all()
    assert {row.person_id for row in attendees} == {first.id}


@pytest.mark.asyncio
async def test_load_agenda_creates_learner_from_stable_si_id(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    write_json(agenda_dir, "index.json", {"PRJ1": {"name": "Courses", "event_count": 1}})
    event = await _basic_event(event_id="123")
    event["learners"] = ["RIVIÈRE Florian"]
    event["learner_ids"] = {"RIVIÈRE Florian": "41581"}
    write_json(agenda_dir, "PRJ1.json", [event])

    await agenda_loader.load_agenda(db_session)
    await agenda_loader.load_agenda(db_session)

    people = (await db_session.execute(select(Person))).scalars().all()
    identities = (await db_session.execute(select(ExternalIdentity))).scalars().all()
    attendees = (await db_session.execute(select(EventAttendee))).scalars().all()
    assert len(people) == len(identities) == len(attendees) == 1
    assert (people[0].first_name, people[0].last_name) == ("Florian", "RIVIÈRE")
    assert identities[0].external_id == "41581"
    assert attendees[0].person_id == people[0].id


@pytest.mark.asyncio
async def test_complete_agenda_snapshot_archives_removed_event(db_session, tmp_path, monkeypatch):
    agenda_dir = _agenda_dir(tmp_path)
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    first = await _basic_event(event_id="first")
    second = await _basic_event(event_id="removed")
    write_json(agenda_dir, "index.json", {"PRJ1": {"name": "Courses", "event_count": 2}})
    write_json(agenda_dir, "PRJ1.json", [first, second])
    await agenda_loader.load_agenda(db_session)

    write_json(agenda_dir, "index.json", {"PRJ1": {
        "name": "Courses", "event_count": 1, "schema_version": 2,
        "start_date": "2026-03-01", "end_date": "2026-03-31",
        "last_sync": "2026-03-01T12:00:00",
    }})
    write_json(agenda_dir, "PRJ1.json", [first])
    write_json(agenda_dir, "scope.json", {
        "status": "complete", "calendar_ids": ["PRJ1"],
        "start_date": "2026-03-01", "end_date": "2026-03-31",
        "last_sync": "2026-03-01T12:00:00",
    })
    await agenda_loader.load_agenda(db_session)

    rows = (await db_session.execute(select(Event.external_ref, Event.is_active))).all()
    assert dict(rows) == {"first": True, "removed": False}
