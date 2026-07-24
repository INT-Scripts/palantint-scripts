import asyncio
import json
import os
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import Student

from palantint_scripts.config import SCRAPS_AUTO_DIR, EXPORTS_DIR

EXPORT_DIR = str(EXPORTS_DIR)

async def load_apartments(db_session: AsyncSession, progress=None, task_id=None, log=print):
    """
    Restores apartment data from the vault (apartments.json) and loads
    Maisel room details from logements.json into the database.
    """
    json_path = os.path.join(EXPORT_DIR, "apartments.json")
    scrap_dir = str(SCRAPS_AUTO_DIR)
    logements_path = os.path.join(scrap_dir, "logements.json")

    # 1. Load Student -> Apartment Mappings
    if os.path.exists(json_path):
        log(f"Restoring precision housing map from [magenta]apartments.json[/magenta]...")
        with open(json_path, "r", encoding="utf-8") as f:
            mapping = json.load(f)
        for trombint_id, apt_no in mapping.items():
            res = await db_session.execute(select(Student).where(Student.trombint_id == trombint_id))
            st = res.scalars().first()
            if st:
                st.apartment = str(apt_no)
    else:
        log("No housing vault map (apartments.json) found. Skipping precision assignment.")

    # 2. Load Maisel Room Details into database
    if os.path.exists(logements_path):
        from db.models import ApartmentDetail
        log(f"Loading room details from [magenta]logements.json[/magenta]...")
        with open(logements_path, "r", encoding="utf-8") as f:
            logements = json.load(f)

        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Load Apartments: Saving {len(logements)} room details...[/blue]", total=len(logements), completed=0)

        for room_id, details in logements.items():
            result = await db_session.execute(select(ApartmentDetail).where(ApartmentDetail.id == str(room_id)))
            apt_detail = result.scalars().first()
            if not apt_detail:
                apt_detail = ApartmentDetail(id=str(room_id))
                db_session.add(apt_detail)

            apt_detail.building = details.get("Bâtiment", "")
            apt_detail.floor = details.get("Etage", "")
            apt_detail.type = details.get("Type")
            apt_detail.surface = details.get("Superficie")
            apt_detail.price = details.get("Tarif")
            apt_detail.alloc_boursier = details.get("Allocation boursier")
            apt_detail.alloc_non_boursier = details.get("Allocation non boursier")
            apt_detail.req_b = int(details.get("_req_b", 0))
            apt_detail.req_e = str(details.get("_req_e", ""))
            
            if progress and task_id: progress.update(task_id, advance=1)
            
        log(f"Loaded details for {len(logements)} apartments.")
    else:
        log("No lodging scraps data (logements.json) found. Skipping room details.")

    await db_session.flush()
    if progress and task_id:
        progress.update(task_id, description="  [green]Load Apartments: Done.[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_apartments(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
