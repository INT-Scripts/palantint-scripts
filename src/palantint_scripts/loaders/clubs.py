import asyncio
import json
import os
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import AsyncSessionLocal
from db.models import Organization, OrganizationLink

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import finish_ingestion_run, start_ingestion_run

DATA_DIR = str(SCRAPS_AUTO_DIR)

SOURCE_CODE = "clubs"


def _kind_for(raw_type: str | None) -> str:
    # MiNET's org.type is one of: "club", "liste" (student-run clubs/lists),
    # "association" (the parent associations — BDE, BDA, ASINT, etc.), or
    # "administration" (the school administration itself, plus deleted-org
    # placeholders like "Organisation Supprimée"). Only the first two are
    # actual clubs; associations are their own kind, and administration
    # entries must never surface as CLUB_KINDS.
    raw_type = (raw_type or "").strip().lower()
    if raw_type == "association":
        return "BUREAU"
    if raw_type == "administration":
        return "ADMIN"
    return "CLUB"


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

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    synced = 0

    try:
        for club_info in clubs_data:
            org_row = {
                "kind": _kind_for(club_info.get("type")),
                "name": club_info["name"],
                "slug": club_info.get("slug"),
                "description": club_info.get("description"),
                "logo_url": club_info.get("logo_url"),
                "color_primary": club_info.get("color_primary"),
                "color_secondary": club_info.get("color_secondary"),
                "attributes": {
                    "type": club_info.get("type"),
                    "association_of_origin": club_info.get("association_of_origin"),
                },
            }

            stmt = insert(Organization).values(**org_row)
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "kind": stmt.excluded.kind,
                    "slug": stmt.excluded.slug,
                    "description": stmt.excluded.description,
                    "logo_url": stmt.excluded.logo_url,
                    "color_primary": stmt.excluded.color_primary,
                    "color_secondary": stmt.excluded.color_secondary,
                    "attributes": stmt.excluded.attributes,
                }
            ).returning(Organization.id)

            result = await db_session.execute(upsert_stmt)
            org_id = result.scalar_one()

            # Sync links
            links = club_info.get("links", [])
            await db_session.execute(delete(OrganizationLink).where(OrganizationLink.organization_id == org_id))

            if links:
                links_rows = [{"organization_id": org_id, "name": l["name"], "url": l["url"]} for l in links]
                await db_session.execute(insert(OrganizationLink), links_rows)

            synced += 1
            if progress and task_id:
                progress.update(task_id, advance=1)

        await db_session.flush()
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=synced)
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Clubs: Done ({len(clubs_data)} organizations).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_clubs(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
