import asyncio
import json
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from db.database import AsyncSessionLocal
from db.models import Student

from palantint_scripts.config import SCRAPS_AUTO_DIR

DATA_DIR = str(SCRAPS_AUTO_DIR)

def _format_name(nom_complet: str):
    parts = nom_complet.split()
    first_parts, last_parts = [], []
    for p in parts:
        if p == p.upper() and len(p) > 1: last_parts.append(p.capitalize())
        elif last_parts: last_parts.append(p.capitalize())
        else: first_parts.append(p)
    return (" ".join(first_parts) or (parts[0] if parts else "")), (" ".join(last_parts) or "")

async def load_trombint(db_session: AsyncSession, progress=None, task_id=None, log=print):
    json_path = os.path.join(DATA_DIR, "students.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Update Students: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        all_students_data = json.load(f)

    if not all_students_data:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Update Students: JSON empty. Skipped.[/yellow]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Update Students: Syncing {len(all_students_data)} records...[/blue]", total=len(all_students_data), completed=0)

    found_uids = set()
    now = datetime.utcnow()

    for s in all_students_data:
        uid = s.get("uid") or s.get("trombint_id")
        if not uid: continue
        found_uids.add(uid)
        
        first_name, last_name = _format_name(s.get("nom_complet", f"{s.get('first_name', '')} {s.get('last_name', '')}"))
        
        # Prepare row values
        val = {
            "trombint_id": uid,
            "first_name": first_name,
            "last_name": last_name,
            "email": s.get("email", ""),
            "profile_picture_path": s.get("photo_url", s.get("profile_picture_path", "")),
            "ecole": s.get("ecole"),
            "promo": s.get("promo"),
            "is_active": True,
            "last_seen_at": now,
        }
        
        stmt = insert(Student).values(**val)
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["trombint_id"],
            set_={k: v for k, v in val.items() if v is not None}
        )
        await db_session.execute(upsert_stmt)
        if progress and task_id: progress.update(task_id, advance=1)
    
    # Safety: only mark inactive if we have a high-confidence batch
    if len(found_uids) > 100:
        await db_session.execute(
            update(Student)
            .where(Student.trombint_id.not_in(list(found_uids)))
            .values(is_active=False)
        )
    
    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Update Students: Done.[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_trombint(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
