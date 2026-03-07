import asyncio
import json
import os
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from db.database import AsyncSessionLocal
from db.models import Student

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../data/scraps"))

def _format_name(nom_complet: str):
    parts = nom_complet.split()
    first_parts, last_parts = [], []
    for p in parts:
        if p == p.upper() and len(p) > 1:
            last_parts.append(p.capitalize())
        elif last_parts:
            last_parts.append(p.capitalize())
        else:
            first_parts.append(p)
    return (" ".join(first_parts) or (parts[0] if parts else "")), (" ".join(last_parts) or "")

async def load_trombint(db_session: AsyncSession, progress=None, task_id=None):
    json_path = os.path.join(DATA_DIR, "trombint.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load TrombINT: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        all_students_data = json.load(f)

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load TrombINT: Syncing {len(all_students_data)} records to DB...[/blue]")

    found_uids = set()
    now = datetime.utcnow()

    for s in all_students_data:
        uid = s.get("uid")
        if not uid: continue
        found_uids.add(uid)
        
        first_name, last_name = _format_name(s.get("nom_complet", ""))
        stmt = insert(Student).values(
            trombint_id=uid,
            first_name=first_name,
            last_name=last_name,
            email=s.get("email", ""),
            profile_picture_path=s.get("photo_url", ""),
            is_active=True,
            last_seen_at=now,
        )
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["trombint_id"],
            set_={
                "first_name": stmt.excluded.first_name,
                "last_name": stmt.excluded.last_name,
                "email": stmt.excluded.email,
                "profile_picture_path": stmt.excluded.profile_picture_path,
                "is_active": True,
                "last_seen_at": now,
            }
        )
        await db_session.execute(upsert_stmt)
    
    # Mark students not found in this scrape as inactive
    await db_session.execute(
        update(Student)
        .where(Student.trombint_id.not_in(list(found_uids)))
        .values(is_active=False)
    )
    
    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description="  [green]Load TrombINT: Database sync complete.[/green]", completed=1, total=1)

async def main():
    async with AsyncSessionLocal() as session:
        await load_trombint(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
