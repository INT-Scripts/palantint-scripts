import pytest
from sqlalchemy import select

from db.models import Event, EventOrganization, EventPresenter, Location, Organization, Person
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
