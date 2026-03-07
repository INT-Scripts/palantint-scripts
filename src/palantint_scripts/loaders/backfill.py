import asyncio
import json
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import Student

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../data/scraps"))

async def load_backfill(db_session: AsyncSession, progress=None, task_id=None, force_reverify: bool = False):
    json_path = os.path.join(DATA_DIR, "backfill.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Backfill: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        student_data = json.load(f)

    if force_reverify:
        result = await db_session.execute(select(Student))
    else:
        result = await db_session.execute(select(Student).where((Student.ecole == None) | (Student.promo == None)))
    
    needing_backfill = result.scalars().all()

    if not needing_backfill:
        if progress and task_id:
            progress.update(task_id, description="  [green]Load Backfill: All students already hydrated.[/green]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description="  [blue]Load Backfill: Updating database...[/blue]")
    
    updated = 0
    for student in needing_backfill:
        info = student_data.get(student.trombint_id)
        has_change = False
        if info:
            if "promo" in info and student.promo != info["promo"]:
                student.promo = info["promo"]
                has_change = True
            if "ecole" in info and student.ecole != info["ecole"]:
                student.ecole = info["ecole"]
                has_change = True
        
        # Infer école from email if still missing
        if not student.ecole and student.email:
            if "imt-bs" in student.email: 
                student.ecole = "Institut Mines-Télécom Business School"
                has_change = True
            elif "telecom-sudparis" in student.email: 
                student.ecole = "Télécom SudParis"
                has_change = True
        
        if has_change:
            updated += 1

    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Backfill: Done (Hydrated {updated} students).[/green]")

async def main():
    import sys
    force = "--force" in sys.argv
    async with AsyncSessionLocal() as session:
        await load_backfill(session, force_reverify=force)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
