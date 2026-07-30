import json
import uuid

import pytest
from sqlalchemy import select

from db.models import (
    Location,
    MapMetadata,
    Media,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
    SocialLink,
)
from palantint_scripts import exporter
from palantint_scripts.db_helpers import get_data_source_id
from palantint_scripts.loaders import vault as vault_loader

from tests.conftest import write_json


async def _make_person_with_trombint(db_session, external_id, first_name="John", last_name="Doe"):
    source_id = await get_data_source_id(db_session, "trombint")
    person = Person(kind="STUDENT", first_name=first_name, last_name=last_name)
    db_session.add(person)
    await db_session.flush()
    from db.models import ExternalIdentity
    db_session.add(ExternalIdentity(person_id=person.id, source_id=source_id, external_id=external_id))
    await db_session.flush()
    return person


@pytest.mark.asyncio
async def test_export_writes_to_temp_dir_not_real_exports(db_session, tmp_path):
    """The most important property of this test file: it never touches the
    real project data/exports/ directory."""
    await exporter.export_db_data(db_session, log=lambda x: None, export_dir=str(tmp_path))
    assert (tmp_path / "socials.json").exists()
    assert (tmp_path / "apartments.json").exists()
    assert (tmp_path / "relationships.json").exists()
    assert (tmp_path / "media.json").exists()
    assert (tmp_path / "maps.json").exists()


@pytest.mark.asyncio
async def test_social_link_export_then_restore_roundtrip(db_session, tmp_path, monkeypatch):
    person = await _make_person_with_trombint(db_session, "jdoe")
    social = SocialLink(person_id=person.id, platform="instagram", username="jdoe_ig", url="https://instagram.com/jdoe_ig", confidence="LIKELY")
    db_session.add(social)
    await db_session.commit()

    await exporter.export_db_data(db_session, log=lambda x: None, export_dir=str(tmp_path))

    exported = json.loads((tmp_path / "socials.json").read_text())
    assert len(exported) == 1
    assert exported[0]["trombint_id"] == "jdoe"
    assert exported[0]["confidence"] == "LIKELY"

    # Simulate restoring into a DB that only has the identity (person), not the social link itself.
    await db_session.execute(SocialLink.__table__.delete())
    await db_session.commit()
    db_session.expire_all()

    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))
    await vault_loader.restore_socials(db_session, log=lambda x: None)

    result = await db_session.execute(select(SocialLink).where(SocialLink.person_id == person.id))
    restored = result.scalars().one()
    assert restored.platform == "instagram"
    assert restored.username == "jdoe_ig"
    assert restored.confidence == "LIKELY"
    assert restored.source_id is not None


@pytest.mark.asyncio
async def test_social_link_restore_is_idempotent(db_session, tmp_path, monkeypatch):
    person = await _make_person_with_trombint(db_session, "jdoe")
    await db_session.commit()

    fixed_id = str(uuid.uuid4())
    write_json(tmp_path, "socials.json", [
        {"id": fixed_id, "trombint_id": "jdoe", "platform": "x", "username": "jdoe_x", "url": "https://x.com/jdoe_x", "confidence": "CONFIRMED"},
    ])
    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))

    await vault_loader.restore_socials(db_session, log=lambda x: None)
    await vault_loader.restore_socials(db_session, log=lambda x: None)

    result = await db_session.execute(select(SocialLink).where(SocialLink.person_id == person.id))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_relationship_export_then_restore_roundtrip_preserves_confidence_and_evidence(db_session, tmp_path, monkeypatch):
    person_a = await _make_person_with_trombint(db_session, "aalpha", "Alice", "Alpha")
    person_b = await _make_person_with_trombint(db_session, "bbeta", "Bob", "Beta")
    rel_type = RelationshipType(name="Test Amis Roundtrip", color="#3b82f6")
    db_session.add(rel_type)
    await db_session.flush()

    media = Media(person_id=person_a.id, kind="IMAGE", file_path="/evidence.jpg")
    db_session.add(media)
    await db_session.flush()
    media_id = media.id

    rel = PersonRelationship(
        person_a_id=person_a.id, person_b_id=person_b.id, relationship_type_id=rel_type.id,
        confidence="CONFIRMED", evidence_media_id=media_id,
    )
    db_session.add(rel)
    await db_session.commit()

    await exporter.export_db_data(db_session, log=lambda x: None, export_dir=str(tmp_path))

    await db_session.execute(PersonRelationship.__table__.delete())
    await db_session.commit()
    db_session.expire_all()

    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))
    await vault_loader.restore_relationships(db_session, log=lambda x: None)

    result = await db_session.execute(select(PersonRelationship))
    restored = result.scalars().one()
    assert {restored.person_a_id, restored.person_b_id} == {person_a.id, person_b.id}
    assert restored.confidence == "CONFIRMED"
    assert restored.evidence_media_id == media_id


@pytest.mark.asyncio
async def test_housing_export_then_restore_roundtrip(db_session, tmp_path, monkeypatch):
    person = await _make_person_with_trombint(db_session, "jdoe")
    apartment = Location(kind="APARTMENT", code="1101")
    db_session.add(apartment)
    await db_session.flush()
    housing_source_id = await get_data_source_id(db_session, "maisel")
    db_session.add(PersonHousing(person_id=person.id, location_id=apartment.id, source_id=housing_source_id))
    await db_session.commit()

    await exporter.export_db_data(db_session, log=lambda x: None, export_dir=str(tmp_path))
    exported = json.loads((tmp_path / "apartments.json").read_text())
    assert exported == {"jdoe": "1101"}

    await db_session.execute(PersonHousing.__table__.delete())
    await db_session.commit()
    db_session.expire_all()

    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))
    await vault_loader.restore_apartment_mappings(db_session, log=lambda x: None)

    result = await db_session.execute(select(PersonHousing).where(PersonHousing.person_id == person.id))
    restored = result.scalars().one()
    assert restored.ended_at is None
    restored_loc = await db_session.get(Location, restored.location_id)
    assert restored_loc.code == "1101"


@pytest.mark.asyncio
async def test_map_metadata_export_then_restore_roundtrip(db_session, tmp_path, monkeypatch):
    building = Location(kind="BUILDING", code="U3")
    db_session.add(building)
    await db_session.flush()
    floor = Location(kind="FLOOR", code="0", parent_id=building.id)
    db_session.add(floor)
    await db_session.flush()
    meta = MapMetadata(location_id=floor.id, pillars=[{"x": 1.0, "y": 2.0}])
    db_session.add(meta)
    await db_session.commit()

    await exporter.export_db_data(db_session, log=lambda x: None, export_dir=str(tmp_path))
    exported = json.loads((tmp_path / "maps.json").read_text())
    assert exported == [{"id": str(meta.id), "building_id": "U3", "floor_id": "0", "pillars": [{"x": 1.0, "y": 2.0}]}]

    await db_session.execute(MapMetadata.__table__.delete())
    await db_session.commit()
    db_session.expire_all()

    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))
    await vault_loader.restore_maps(db_session, log=lambda x: None)

    result = await db_session.execute(select(MapMetadata))
    restored = result.scalars().one()
    assert restored.pillars == [{"x": 1.0, "y": 2.0}]
    restored_floor = await db_session.get(Location, restored.location_id)
    assert restored_floor.code == "0"
    restored_building = await db_session.get(Location, restored_floor.parent_id)
    assert restored_building.code == "U3"


@pytest.mark.asyncio
async def test_restore_functions_are_noop_on_missing_files(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(tmp_path))
    await vault_loader.restore_socials(db_session, log=lambda x: None)
    await vault_loader.restore_relationships(db_session, log=lambda x: None)
    await vault_loader.restore_media(db_session, log=lambda x: None)
    await vault_loader.restore_apartment_mappings(db_session, log=lambda x: None)
    await vault_loader.restore_maps(db_session, log=lambda x: None)
    # No exception raised is the assertion here.
