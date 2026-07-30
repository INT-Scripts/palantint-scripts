import pytest
from sqlalchemy import select

from db.models import Location, PersonHousing
from palantint_scripts.loaders import apartments as apartments_loader
from palantint_scripts.loaders import trombint as trombint_loader

from tests.conftest import write_json


@pytest.mark.asyncio
async def test_load_apartments_no_files_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))
    await apartments_loader.load_apartments(db_session)
    result = await db_session.execute(select(Location))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_apartments_room_details_create_location_with_attributes_and_parent(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))
    write_json(tmp_path, "logements.json", {
        "1101": {
            "Bâtiment": "U1", "Etage": "Premier étage", "Type": "studio",
            "Superficie": "22 m²", "Tarif": "550€",
            "Allocation boursier": "114€", "Allocation non boursier": "87€",
            "_req_b": 1, "_req_e": "x",
        }
    })
    await apartments_loader.load_apartments(db_session)

    result = await db_session.execute(select(Location).where(Location.kind == "APARTMENT"))
    apt = result.scalars().one()
    assert apt.code == "1101"
    assert apt.attributes["type"] == "studio"
    assert apt.attributes["surface"] == "22 m²"

    building = await db_session.get(Location, apt.parent_id)
    assert building.kind == "BUILDING"
    assert building.code == "U1"


@pytest.mark.asyncio
async def test_load_apartments_reassignment_closes_prior_active_housing(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "students.json", [{"uid": "jdoe", "nom_complet": "John DOE"}])
    await trombint_loader.load_trombint(db_session)

    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))

    write_json(tmp_path, "apartments.json", {"jdoe": "1101"})
    await apartments_loader.load_apartments(db_session)

    write_json(tmp_path, "apartments.json", {"jdoe": "2202"})
    await apartments_loader.load_apartments(db_session)

    result = await db_session.execute(select(PersonHousing))
    rows = result.scalars().all()
    assert len(rows) == 2

    active = [r for r in rows if r.ended_at is None]
    closed = [r for r in rows if r.ended_at is not None]
    assert len(active) == 1
    assert len(closed) == 1

    active_loc = await db_session.get(Location, active[0].location_id)
    closed_loc = await db_session.get(Location, closed[0].location_id)
    assert active_loc.code == "2202"
    assert closed_loc.code == "1101"


@pytest.mark.asyncio
async def test_load_apartments_reassign_same_apartment_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "students.json", [{"uid": "jdoe", "nom_complet": "John DOE"}])
    await trombint_loader.load_trombint(db_session)

    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))
    write_json(tmp_path, "apartments.json", {"jdoe": "1101"})

    await apartments_loader.load_apartments(db_session)
    await apartments_loader.load_apartments(db_session)

    result = await db_session.execute(select(PersonHousing))
    rows = result.scalars().all()
    assert len(rows) == 1, "re-syncing the same assignment must not churn a new PersonHousing row"
    assert rows[0].ended_at is None


@pytest.mark.asyncio
async def test_load_apartments_unknown_trombint_id_is_skipped(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))
    write_json(tmp_path, "apartments.json", {"ghost-uid": "1101"})
    await apartments_loader.load_apartments(db_session)

    result = await db_session.execute(select(PersonHousing))
    assert result.scalars().all() == []
