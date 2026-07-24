import asyncio
import json
import os
import uuid
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import (
    MapMetadata, ThreeDConfig, StudentRelationship, SocialLink, Media, 
    Student, RelationshipType, User
)

EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports"))

def _is_uuid(val):
    if not isinstance(val, str): return False
    try:
        uuid.UUID(val)
        return True
    except Exception:
        return False

async def restore_maps(db_session: AsyncSession, log=print):
    """Restores MapMetadata calibrations from maps.json."""
    json_path = os.path.join(EXPORT_DIR, "maps.json")
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]Map Calibrations[/magenta] ({len(data)} records)...")
    for item in data:
        building_id = item.get("building_id")
        floor_id = item.get("floor_id")
        pillars = item.get("pillars", [])
        if not building_id or not floor_id: continue

        res = await db_session.execute(
            select(MapMetadata).where(
                MapMetadata.building_id == building_id,
                MapMetadata.floor_id == floor_id
            )
        )
        meta = res.scalars().first()
        if not meta:
            meta = MapMetadata(
                id=uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4(),
                building_id=building_id,
                floor_id=floor_id,
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
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]Social Handles[/magenta] ({len(data)} records)...")
    count = 0
    for item in data:
        trombint_id = item.get("trombint_id")
        raw_student_id = item.get("student_id")
        
        student = None
        if trombint_id:
            res = await db_session.execute(select(Student).where(Student.trombint_id == trombint_id))
            student = res.scalars().first()
        if not student and _is_uuid(raw_student_id):
            student = await db_session.get(Student, uuid.UUID(raw_student_id))
        
        if not student: continue

        sid = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        link_obj = await db_session.get(SocialLink, sid)
        if not link_obj:
            link_obj = SocialLink(
                id=sid,
                student_id=student.id,
                platform=item.get("platform", ""),
                username=item.get("username", ""),
                url=item.get("url", "")
            )
            db_session.add(link_obj)
        else:
            link_obj.student_id = student.id
            link_obj.platform = item.get("platform", link_obj.platform)
            link_obj.username = item.get("username", link_obj.username)
            link_obj.url = item.get("url", link_obj.url)
        count += 1
    await db_session.flush()

async def restore_relationships(db_session: AsyncSession, log=print):
    """Restores StudentRelationship social graph links from relationships.json."""
    json_path = os.path.join(EXPORT_DIR, "relationships.json")
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]Social Graph Vault[/magenta] ({len(data)} records)...")
    count = 0
    for item in data:
        # Resolve Student A
        st_a = None
        if item.get("student_a_trombint_id"):
            res = await db_session.execute(select(Student).where(Student.trombint_id == item["student_a_trombint_id"]))
            st_a = res.scalars().first()
        if not st_a and _is_uuid(item.get("student_a_id")):
            st_a = await db_session.get(Student, uuid.UUID(item["student_a_id"]))

        # Resolve Student B
        st_b = None
        if item.get("student_b_trombint_id"):
            res = await db_session.execute(select(Student).where(Student.trombint_id == item["student_b_trombint_id"]))
            st_b = res.scalars().first()
        if not st_b and _is_uuid(item.get("student_b_id")):
            st_b = await db_session.get(Student, uuid.UUID(item["student_b_id"]))

        if not st_a or not st_b: continue

        # Resolve RelationshipType
        type_obj = None
        if item.get("relationship_type_name"):
            res_t = await db_session.execute(select(RelationshipType).where(RelationshipType.name == item["relationship_type_name"]))
            type_obj = res_t.scalars().first()
        if not type_obj and _is_uuid(item.get("relationship_type_id")):
            type_obj = await db_session.get(RelationshipType, uuid.UUID(item["relationship_type_id"]))

        if not type_obj: continue

        rel_id = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        rel_obj = await db_session.get(StudentRelationship, rel_id)
        
        created_at = datetime.fromisoformat(item["created_at"]) if item.get("created_at") else datetime.utcnow()

        if not rel_obj:
            rel_obj = StudentRelationship(
                id=rel_id,
                student_a_id=st_a.id,
                student_b_id=st_b.id,
                relationship_type_id=type_obj.id,
                created_at=created_at
            )
            db_session.add(rel_obj)
        else:
            rel_obj.student_a_id = st_a.id
            rel_obj.student_b_id = st_b.id
            rel_obj.relationship_type_id = type_obj.id
        count += 1
    await db_session.flush()

async def restore_media(db_session: AsyncSession, log=print):
    """Restores Media notes and comms logs from media.json."""
    json_path = os.path.join(EXPORT_DIR, "media.json")
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]Comms Log & Media[/magenta] ({len(data)} records)...")
    count = 0
    for item in data:
        trombint_id = item.get("trombint_id")
        student = None
        if trombint_id:
            res = await db_session.execute(select(Student).where(Student.trombint_id == trombint_id))
            student = res.scalars().first()
        if not student and _is_uuid(item.get("student_id")):
            student = await db_session.get(Student, uuid.UUID(item["student_id"]))

        if not student: continue

        m_id = uuid.UUID(item["id"]) if _is_uuid(item.get("id")) else uuid.uuid4()
        media_obj = await db_session.get(Media, m_id)

        uploader_id = uuid.UUID(item["uploaded_by_user_id"]) if _is_uuid(item.get("uploaded_by_user_id")) else None
        if uploader_id:
            u_check = await db_session.get(User, uploader_id)
            if not u_check: uploader_id = None

        uploaded_at = datetime.fromisoformat(item["uploaded_at"]) if item.get("uploaded_at") else datetime.utcnow()

        if not media_obj:
            media_obj = Media(
                id=m_id,
                student_id=student.id,
                type=item.get("type", "NOTE"),
                file_path=item.get("file_path"),
                content=item.get("content"),
                author_name=item.get("author_name"),
                uploaded_by_user_id=uploader_id,
                uploaded_at=uploaded_at
            )
            db_session.add(media_obj)
        else:
            media_obj.student_id = student.id
            media_obj.type = item.get("type", media_obj.type)
            media_obj.file_path = item.get("file_path", media_obj.file_path)
            media_obj.content = item.get("content", media_obj.content)
            media_obj.author_name = item.get("author_name", media_obj.author_name)
            media_obj.uploaded_by_user_id = uploader_id
        count += 1
    await db_session.flush()

async def restore_apartment_mappings(db_session: AsyncSession, log=print):
    """Restores precision student housing assignments from apartments.json."""
    json_path = os.path.join(EXPORT_DIR, "apartments.json")
    if not os.path.exists(json_path): return
    with open(json_path, "r", encoding="utf-8") as f:
        mapping = json.load(f)
    if not mapping: return

    log(f"Restoring [magenta]Precision Housing Map[/magenta] ({len(mapping)} assignments)...")
    count = 0
    for trombint_id, apt_no in mapping.items():
        res = await db_session.execute(select(Student).where(Student.trombint_id == trombint_id))
        st = res.scalars().first()
        if st:
            st.apartment = str(apt_no)
            count += 1
    await db_session.flush()

async def anchor_identities(db_session: AsyncSession, log=print):
    """Pass 1 compatibility anchor (restores precision housing map)."""
    await restore_apartment_mappings(db_session, log)

async def restore_research(db_session: AsyncSession, log=print):
    """Pass 3: Restores all human OSINT intelligence & map calibrations from vault."""
    await restore_maps(db_session, log)
    await restore_3d_config(db_session, log)
    await restore_socials(db_session, log)
    await restore_relationships(db_session, log)
    await restore_media(db_session, log)
    await restore_apartment_mappings(db_session, log)

async def load_vault(db_session: AsyncSession, progress=None, task_id=None, log=print):
    if progress and task_id:
        progress.update(task_id, description="  [blue]Restore Backup: Hydrating OSINT vault...[/blue]", total=1, completed=0)
    await restore_research(db_session, log)
    if progress and task_id:
        progress.update(task_id, description="  [green]Restore Backup: Done.[/green]", completed=1, total=1)
