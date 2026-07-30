import asyncio
import json
import os
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import ExternalIdentity, Location, Person

from palantint_scripts.config import SCRAPS_AUTO_DIR, EXPORTS_DIR
from palantint_scripts.db_helpers import (
    finish_ingestion_run,
    get_data_source_id,
    start_ingestion_run,
)

EXPORT_DIR = str(EXPORTS_DIR)

MAISEL_SOURCE = "maisel"
VAULT_SOURCE = "vault_manual"


async def _get_or_create_building(db_session: AsyncSession, code: str) -> Location:
    code = (code or "").strip() or "UNKNOWN"
    result = await db_session.execute(
        select(Location).where(Location.kind == "BUILDING", Location.code == code)
    )
    building = result.scalars().first()
    if not building:
        building = Location(kind="BUILDING", code=code, name=code)
        db_session.add(building)
        await db_session.flush()
    return building


async def _get_or_create_apartment(db_session: AsyncSession, code: str) -> Location:
    result = await db_session.execute(
        select(Location).where(Location.kind == "APARTMENT", Location.code == code)
    )
    apartment = result.scalars().first()
    if not apartment:
        apartment = Location(kind="APARTMENT", code=code)
        db_session.add(apartment)
        await db_session.flush()
    return apartment


async def _get_active_housing(db_session: AsyncSession, person_id) -> Location | None:
    from db.models import PersonHousing
    result = await db_session.execute(
        select(PersonHousing).where(PersonHousing.person_id == person_id, PersonHousing.ended_at.is_(None))
    )
    return result.scalars().first()


async def _assign_housing(db_session: AsyncSession, person_id, location: Location, source_code: str):
    from db.models import PersonHousing
    from palantint_scripts.db_helpers import utc_now

    current = await _get_active_housing(db_session, person_id)
    if current and current.location_id == location.id:
        return  # already housed here, nothing to do
    if current:
        current.ended_at = utc_now()

    source_id = await get_data_source_id(db_session, source_code)
    db_session.add(PersonHousing(person_id=person_id, location_id=location.id, source_id=source_id))


async def load_apartments(db_session: AsyncSession, progress=None, task_id=None, log=print):
    """
    Restores apartment data from the vault (apartments.json) and loads
    Maisel room details from logements.json into the database.
    """
    json_path = os.path.join(EXPORT_DIR, "apartments.json")
    scrap_dir = str(SCRAPS_AUTO_DIR)
    logements_path = os.path.join(scrap_dir, "logements.json")

    run = await start_ingestion_run(db_session, MAISEL_SOURCE)
    updated_count = 0

    try:
        # 1. Load Maisel Room Details into the Location(kind=APARTMENT) tree first,
        # so the precision housing map below has real Location rows to point at.
        if os.path.exists(logements_path):
            log(f"Loading room details from [magenta]logements.json[/magenta]...")
            with open(logements_path, "r", encoding="utf-8") as f:
                logements = json.load(f)

            if progress and task_id:
                progress.update(task_id, description=f"  [blue]Load Apartments: Saving {len(logements)} room details...[/blue]", total=len(logements), completed=0)

            for room_id, details in logements.items():
                building = await _get_or_create_building(db_session, details.get("Bâtiment", ""))
                apartment = await _get_or_create_apartment(db_session, str(room_id))
                apartment.parent_id = building.id
                apartment.attributes = {
                    "floor": details.get("Etage", ""),
                    "type": details.get("Type"),
                    "surface": details.get("Superficie"),
                    "price": details.get("Tarif"),
                    "alloc_boursier": details.get("Allocation boursier"),
                    "alloc_non_boursier": details.get("Allocation non boursier"),
                    "req_b": int(details.get("_req_b", 0)),
                    "req_e": str(details.get("_req_e", "")),
                }
                updated_count += 1

                if progress and task_id: progress.update(task_id, advance=1)

            log(f"Loaded details for {len(logements)} apartments.")
        else:
            log("No lodging scraps data (logements.json) found. Skipping room details.")

        # 2. Load Student -> Apartment Mappings (precision housing, restored from the vault export)
        if os.path.exists(json_path):
            log(f"Restoring precision housing map from [magenta]apartments.json[/magenta]...")
            with open(json_path, "r", encoding="utf-8") as f:
                mapping = json.load(f)

            trombint_source_id = await get_data_source_id(db_session, "trombint")
            for trombint_id, apt_no in mapping.items():
                res = await db_session.execute(
                    select(Person)
                    .join(ExternalIdentity, ExternalIdentity.person_id == Person.id)
                    .where(
                        ExternalIdentity.source_id == trombint_source_id,
                        ExternalIdentity.external_id == trombint_id,
                    )
                )
                person = res.scalars().first()
                if not person:
                    continue

                apartment = await _get_or_create_apartment(db_session, str(apt_no))
                await _assign_housing(db_session, person.id, apartment, VAULT_SOURCE)
                updated_count += 1
        else:
            log("No housing vault map (apartments.json) found. Skipping precision assignment.")

        await db_session.flush()
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=updated_count)
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description="  [green]Load Apartments: Done.[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_apartments(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
