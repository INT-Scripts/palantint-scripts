import asyncio
import json
import os
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import AsyncSessionLocal
from db.models import Club, ClubLink

from palantint_scripts.config import SCRAPS_AUTO_DIR

DATA_DIR = str(SCRAPS_AUTO_DIR)

async def load_clubs(db_session: AsyncSession, progress=None, task_id=None, log=print):
    json_path = os.path.join(DATA_DIR, "clubs.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Clubs: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        clubs_data = json.load(f)

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Clubs: Syncing {len(clubs_data)} records...[/blue]", total=len(clubs_data), completed=0)

    log(f"Syncing [cyan]{len(clubs_data)}[/cyan] organizations to database...")

    for club_info in clubs_data:
        # Prepare club data for upsert
        club_row = {
            "name": club_info["name"],
            "slug": club_info.get("slug"),
            "description": club_info.get("description"),
            "logo_url": club_info.get("logo_url"),
            "type": club_info.get("type"),
            "association_of_origin": club_info.get("association_of_origin"),
            "color_primary": club_info.get("color_primary"),
            "color_secondary": club_info.get("color_secondary"),
        }
        
        stmt = insert(Club).values(**club_row)
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["name"],
            set_={
                "slug": stmt.excluded.slug,
                "description": stmt.excluded.description,
                "logo_url": stmt.excluded.logo_url,
                "type": stmt.excluded.type,
                "association_of_origin": stmt.excluded.association_of_origin,
                "color_primary": stmt.excluded.color_primary,
                "color_secondary": stmt.excluded.color_secondary,
            }
        ).returning(Club.id)
        
        result = await db_session.execute(upsert_stmt)
        club_id = result.scalar_one()
        
        # Sync links
        links = club_info.get("links", [])
        await db_session.execute(delete(ClubLink).where(ClubLink.club_id == club_id))
        
        if links:
            links_rows = [{"club_id": club_id, "name": l["name"], "url": l["url"]} for l in links]
            await db_session.execute(insert(ClubLink), links_rows)

        if progress and task_id:
            progress.update(task_id, advance=1)

    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Clubs: Done ({len(clubs_data)} organizations).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_clubs(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
