import asyncio
import json
import os
import uuid
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import (
    DataSource,
    ExternalIdentity,
    Location,
    MapMetadata,
    Media,
    Person,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
    SocialLink,
    ThreeDConfig,
    User,
)
from palantint_scripts.db_helpers import finish_ingestion_run, get_data_source_id, start_ingestion_run, utc_now

EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports"))

SOURCE_CODE = "vault_manual"

def _is_uuid(val):
    if not isinstance(val, str): return False
    try:
        uuid.UUID(val)
        return True
    except Exception:
        return False


async def _get_person_by_trombint(db_session: AsyncSession, trombint_id: str) -> Person | None:
    if not trombint_id:
        return None
    result = await db_session.execute(
        select(Person)
        .join(ExternalIdentity, ExternalIdentity.person_id == Person.id)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(DataSource.code == "trombint", ExternalIdentity.external_id == trombint_id)
    )
    return result.scalars().first()


async def _get_or_create_location(db_session: AsyncSession, kind: str, code: str, parent_id=None) -> Location | None:
    if not code:
        return None
    stmt = select(Location).where(Location.kind == kind, Location.code == code)
    if parent_id is not None:
        stmt = stmt.where(Location.parent_id == parent_id)
    result = await db_session.execute(stmt)
    loc = result.scalars().first()
    if not loc:
        loc = Location(kind=kind, code=code, name=code, parent_id=parent_id)
        db_session.add(loc)
        await db_session.flush()
    return loc


async def restore_maps(db_session: AsyncSession, log=print):
    """Restores MapMetadata calibrations from maps.json."""
    json_path = os.path.join(EXPORT_DIR, "maps.json")
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]Map Calibrations[/magenta] ({len(data)} records)...")
    for item in data:
        building_code = item.get("building_id")
        floor_code = item.get("floor_id")
        pillars = item.get("pillars", [])
        if not building_code or not floor_code: continue

        building = await _get_or_create_location(db_session, "BUILDING", building_code)
        floor = await _get_or_create_location(db_session, "FLOOR", floor_code, parent_id=building.id if building else None)
        if not floor: continue

        res = await db_session.execute(select(MapMetadata).where(MapMetadata.location_id == floor.id))
        meta = res.scalars().first()
        if not meta:
            meta = MapMetadata(
                id=uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4(),
                location_id=floor.id,
                pillars=pillars
            )
            db_session.add(meta)
        else:
            meta.pillars = pillars
    await db_session.flush()

async def restore_3d_config(db_session: AsyncSession, log=print):
    """Restores 3D map tile mappings and waypoints from 3d_config.json into database."""
    json_path = os.path.join(EXPORT_DIR, "3d_config.json")
    if not os.path.exists(json_path):
        json_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/assets/3d/config.json"))
    if not os.path.exists(json_path): return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log("Restoring [magenta]3D Map Tile Mappings & Waypoints[/magenta]...")
    res = await db_session.execute(select(ThreeDConfig).where(ThreeDConfig.key == "default"))
    cfg = res.scalars().first()
    if not cfg:
        cfg = ThreeDConfig(
            key="default",
            tile_mappings=data.get("tile_mappings", {}),
            markers=data.get("markers", [])
        )
        db_session.add(cfg)
    else:
        cfg.tile_mappings = data.get("tile_mappings", cfg.tile_mappings)
        cfg.markers = data.get("markers", cfg.markers)
    await db_session.flush()

async def restore_socials(db_session: AsyncSession, log=print):
    """Restores SocialLink OSINT research handles from socials.json."""
    json_path = os.path.join(EXPORT_DIR, "socials.json")
    if not os.path.exists(json_path): return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return 0

    log(f"Restoring [magenta]Social Handles[/magenta] ({len(data)} records)...")
    source_id = await get_data_source_id(db_session, SOURCE_CODE)
    count = 0
    for item in data:
        person = await _get_person_by_trombint(db_session, item.get("trombint_id"))
        if not person and _is_uuid(item.get("person_id")):
            person = await db_session.get(Person, uuid.UUID(item["person_id"]))

        if not person: continue

        sid = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        link_obj = await db_session.get(SocialLink, sid)
        if not link_obj:
            link_obj = SocialLink(
                id=sid,
                person_id=person.id,
                platform=item.get("platform", ""),
                username=item.get("username", ""),
                url=item.get("url", ""),
                source_id=source_id,
                confidence=item.get("confidence", "CONFIRMED"),
            )
            db_session.add(link_obj)
        else:
            link_obj.person_id = person.id
            link_obj.platform = item.get("platform", link_obj.platform)
            link_obj.username = item.get("username", link_obj.username)
            link_obj.url = item.get("url", link_obj.url)
            link_obj.source_id = source_id
            link_obj.confidence = item.get("confidence", link_obj.confidence)
        count += 1
    await db_session.flush()
    return count

async def restore_relationships(db_session: AsyncSession, log=print):
    """Restores PersonRelationship social graph links from relationships.json."""
    json_path = os.path.join(EXPORT_DIR, "relationships.json")
    if not os.path.exists(json_path): return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return 0

    log(f"Restoring [magenta]Social Graph Vault[/magenta] ({len(data)} records)...")
    source_id = await get_data_source_id(db_session, SOURCE_CODE)
    count = 0
    for item in data:
        person_a = await _get_person_by_trombint(db_session, item.get("person_a_trombint_id"))
        if not person_a and _is_uuid(item.get("person_a_id")):
            person_a = await db_session.get(Person, uuid.UUID(item["person_a_id"]))

        person_b = await _get_person_by_trombint(db_session, item.get("person_b_trombint_id"))
        if not person_b and _is_uuid(item.get("person_b_id")):
            person_b = await db_session.get(Person, uuid.UUID(item["person_b_id"]))

        if not person_a or not person_b: continue

        type_obj = None
        if item.get("relationship_type_name"):
            res_t = await db_session.execute(select(RelationshipType).where(RelationshipType.name == item["relationship_type_name"]))
            type_obj = res_t.scalars().first()
        if not type_obj and _is_uuid(item.get("relationship_type_id")):
            type_obj = await db_session.get(RelationshipType, uuid.UUID(item["relationship_type_id"]))

        if not type_obj: continue

        rel_id = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        rel_obj = await db_session.get(PersonRelationship, rel_id)

        created_at = datetime.fromisoformat(item["created_at"]) if item.get("created_at") else utc_now()

        evidence_media_id = uuid.UUID(item["evidence_media_id"]) if _is_uuid(item.get("evidence_media_id")) else None

        if not rel_obj:
            rel_obj = PersonRelationship(
                id=rel_id,
                person_a_id=person_a.id,
                person_b_id=person_b.id,
                relationship_type_id=type_obj.id,
                confidence=item.get("confidence", "LIKELY"),
                evidence_media_id=evidence_media_id,
                source_id=source_id,
                created_at=created_at
            )
            db_session.add(rel_obj)
        else:
            rel_obj.person_a_id = person_a.id
            rel_obj.person_b_id = person_b.id
            rel_obj.relationship_type_id = type_obj.id
            rel_obj.confidence = item.get("confidence", rel_obj.confidence)
            rel_obj.evidence_media_id = evidence_media_id
            rel_obj.source_id = source_id
        count += 1
    await db_session.flush()
    return count

async def restore_media(db_session: AsyncSession, log=print):
    """Restores Media notes and comms logs from media.json."""
    json_path = os.path.join(EXPORT_DIR, "media.json")
    if not os.path.exists(json_path): return 0
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return 0

    log(f"Restoring [magenta]Comms Log & Media[/magenta] ({len(data)} records)...")
    source_id = await get_data_source_id(db_session, SOURCE_CODE)
    count = 0
    for item in data:
        person = await _get_person_by_trombint(db_session, item.get("trombint_id"))
        if not person and _is_uuid(item.get("person_id")):
            person = await db_session.get(Person, uuid.UUID(item["person_id"]))

        if not person: continue

        m_id = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        media_obj = await db_session.get(Media, m_id)

        uploader_id = uuid.UUID(item["uploaded_by_user_id"]) if _is_uuid(item.get("uploaded_by_user_id")) else None
        if uploader_id:
            u_check = await db_session.get(User, uploader_id)
            if not u_check: uploader_id = None

        uploaded_at = datetime.fromisoformat(item["uploaded_at"]) if item.get("uploaded_at") else utc_now()

        if not media_obj:
            media_obj = Media(
                id=m_id,
                person_id=person.id,
                kind=item.get("type", "NOTE"),
                file_path=item.get("file_path"),
                content=item.get("content"),
                author_name=item.get("author_name"),
                uploaded_by_user_id=uploader_id,
                source_id=source_id,
                uploaded_at=uploaded_at
            )
            db_session.add(media_obj)
        else:
            media_obj.person_id = person.id
            media_obj.kind = item.get("type", media_obj.kind)
            media_obj.file_path = item.get("file_path", media_obj.file_path)
            media_obj.content = item.get("content", media_obj.content)
            media_obj.author_name = item.get("author_name", media_obj.author_name)
            media_obj.uploaded_by_user_id = uploader_id
            media_obj.source_id = source_id
        count += 1
    await db_session.flush()
    return count

async def restore_apartment_mappings(db_session: AsyncSession, log=print):
    """Restores precision person housing assignments from apartments.json."""
    json_path = os.path.join(EXPORT_DIR, "apartments.json")
    if not os.path.exists(json_path): return 0
    with open(json_path, "r", encoding="utf-8") as f:
        mapping = json.load(f)
    if not mapping: return 0

    log(f"Restoring [magenta]Precision Housing Map[/magenta] ({len(mapping)} assignments)...")
    source_id = await get_data_source_id(db_session, SOURCE_CODE)
    count = 0
    for trombint_id, apt_no in mapping.items():
        person = await _get_person_by_trombint(db_session, trombint_id)
        if not person: continue

        apartment = await _get_or_create_location(db_session, "APARTMENT", str(apt_no))

        res = await db_session.execute(
            select(PersonHousing).where(PersonHousing.person_id == person.id, PersonHousing.ended_at.is_(None))
        )
        current = res.scalars().first()
        if current and current.location_id == apartment.id:
            continue
        if current:
            current.ended_at = utc_now()
        db_session.add(PersonHousing(person_id=person.id, location_id=apartment.id, source_id=source_id))
        count += 1
    await db_session.flush()
    return count

async def anchor_identities(db_session: AsyncSession, log=print):
    """Pass 1 compatibility anchor (restores precision housing map)."""
    await restore_apartment_mappings(db_session, log)

async def restore_research(db_session: AsyncSession, log=print):
    """Pass 3: Restores all human OSINT intelligence & map calibrations from vault."""
    await restore_maps(db_session, log)
    await restore_3d_config(db_session, log)
    socials_n = await restore_socials(db_session, log)
    rel_n = await restore_relationships(db_session, log)
    media_n = await restore_media(db_session, log)
    housing_n = await restore_apartment_mappings(db_session, log)
    return socials_n + rel_n + media_n + housing_n

async def load_vault(db_session: AsyncSession, progress=None, task_id=None, log=print):
    if progress and task_id:
        progress.update(task_id, description="  [blue]Restore Backup: Hydrating OSINT vault...[/blue]", total=1, completed=0)

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    try:
        updated = await restore_research(db_session, log)
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=updated)
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description="  [green]Restore Backup: Done.[/green]", completed=1, total=1)

async def main():
    async with AsyncSessionLocal() as session:
        await load_vault(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
