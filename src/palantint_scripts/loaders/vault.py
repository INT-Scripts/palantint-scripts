import asyncio
import json
import os
import uuid
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from db.database import AsyncSessionLocal
from db.models import (
    MapMetadata, StudentRelationship, SocialLink, Media, 
    Student, Club, StudentClub, ClubLink, RelationshipType, User
)

EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports"))

def _is_uuid(val):
    if not isinstance(val, str): return False
    try:
        uuid.UUID(val)
        return True
    except:
        return False

async def restore_vault_table(db_session: AsyncSession, model, filename: str, desc: str, log):
    json_path = os.path.join(EXPORT_DIR, filename)
    if not os.path.exists(json_path): return

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not data: return

    log(f"Restoring [magenta]{desc}[/magenta] ({len(data)} records)...")
    
    with db_session.no_autoflush:
        for record in data:
            try:
                # 1. Type Conversion
                for key, val in record.items():
                    if isinstance(val, str) and val:
                        if key.endswith("_at") or key.endswith("_time") or key == "viewed_at":
                            try: record[key] = datetime.fromisoformat(val)
                            except: pass
                        elif _is_uuid(val):
                            record[key] = uuid.UUID(val)

                # 2. Hard-Validation & Foreign Key Protection
                if model not in (Student, Club):
                    # Validate Student existence
                    if "student_id" in record:
                        s = await db_session.get(Student, record["student_id"])
                        if not s: continue
                    
                    if "student_a_id" in record:
                        sa = await db_session.get(Student, record["student_a_id"])
                        sb = await db_session.get(Student, record["student_b_id"])
                        if not sa or not sb: continue
                    
                    # Validate Club existence
                    if "club_id" in record:
                        c = await db_session.get(Club, record["club_id"])
                        if not c: continue
                    
                    # Validate Relationship Types
                    if "relationship_type_id" in record:
                        rt = await db_session.get(RelationshipType, record["relationship_type_id"])
                        if not rt:
                            log(f"[yellow]Warning: Missing type {record['relationship_type_id']}. Skipping relation.[/yellow]")
                            continue
                    
                    # Validate User existence (Media Author)
                    if "uploaded_by_user_id" in record and record["uploaded_by_user_id"]:
                        u = await db_session.get(User, record["uploaded_by_user_id"])
                        if not u:
                            # Strip the invalid user ID but keep the media record
                            record["uploaded_by_user_id"] = None

                # 3. Atomic Merge
                obj = model(**record)
                await db_session.merge(obj)
            except Exception as e:
                # log(f"[red]Error in {desc} record: {e}[/red]")
                continue

    await db_session.flush()

async def anchor_identities(db_session: AsyncSession, log=print):
    """
    PASS 1: Restores Student and Club registries from the vault.
    """
    targets = [
        (Student, "students.json", "Identity Registry (UUID Anchors)"),
        (Club, "clubs.json", "Organization Registry"),
    ]
    for model, filename, desc in targets:
        await restore_vault_table(db_session, model, filename, desc, log)

async def restore_research(db_session: AsyncSession, log=print):
    """
    PASS 3: Restores manual OSINT research and infrastructure calibrations.
    """
    targets = [
        (MapMetadata, "maps.json", "Map Calibrations"),
        (SocialLink, "socials.json", "Social Handles"),
        (StudentRelationship, "relationships.json", "Social Graph"),
        (Media, "media.json", "Comms Log & Media"),
        (ClubLink, "club_links.json", "Organization Links"),
        (StudentClub, "memberships.json", "Subject-Organization Ties"),
    ]
    for model, filename, desc in targets:
        await restore_vault_table(db_session, model, filename, desc, log)

async def load_vault(db_session: AsyncSession, progress=None, task_id=None, log=print):
    if progress and task_id:
        progress.update(task_id, description="  [blue]Restore Backup: Hydrating OSINT vault...[/blue]", total=2, completed=0)
    
    await anchor_identities(db_session, log)
    if progress and task_id: progress.update(task_id, advance=1)
    
    await restore_research(db_session, log)
    if progress and task_id: progress.update(task_id, advance=1)

    if progress and task_id:
        progress.update(task_id, description="  [green]Restore Backup: Done.[/green]")
