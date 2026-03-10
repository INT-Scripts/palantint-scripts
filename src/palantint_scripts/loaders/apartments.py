import asyncio
import json
import os
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import Student

EXPORT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/exports"))

async def load_apartments(db_session: AsyncSession, progress=None, task_id=None, log=print):
    """
    Restores apartment data from the vault (apartments.json).
    """
    json_path = os.path.join(EXPORT_DIR, "apartments.json")
    
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Apartments: No vault data found. Skipping.[/yellow]", completed=1, total=1)
        return

    log(f"Restoring precision housing map from [magenta]apartments.json[/magenta]...")
    with open(json_path, "r", encoding="utf-8") as f:
        mapping = json.load(f)
        
    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Apartments: Restoring {len(mapping)} mappings...[/blue]", total=len(mapping), completed=0)
        
    matched = 0
    for tid, apt_num in mapping.items():
        result = await db_session.execute(select(Student).where(Student.trombint_id == tid))
        student = result.scalars().first()
        if student:
            # We only update if the current field is empty to avoid overwriting newer scrapes
            # OR we can assume vault data is higher authority
            student.apartment = str(apt_num)
            matched += 1
        if progress and task_id: progress.update(task_id, advance=1)
        
    await db_session.flush()
    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Apartments: Restored {matched}/{len(mapping)} mappings.[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_apartments(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(load_apartments())
