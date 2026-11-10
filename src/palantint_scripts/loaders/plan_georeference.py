"""Persist the geographic placement of the historical floor plans on their FLOOR locations.

The harvest step (``palantint_scripts.plan_registration``) derives one similarity
transform per drawing from Wemap. This loader stores it, with its method and
quality, in ``Location.attributes.plan_georeference`` of the seeded plan floor.
Other attributes, identities and the floor's own provenance are preserved; the
placement carries the ``plan_georeference`` source in its own ``source_id``.
"""

import asyncio
import json

from sqlalchemy import select
from sqlalchemy.orm import aliased

from db.database import AsyncSessionLocal
from db.models import Location
from palantint_scripts.config import PLAN_REGISTRATION_PATH
from palantint_scripts.db_helpers import finish_ingestion_run, get_data_source_id, start_ingestion_run

SOURCE = "plan_georeference"


def placement(name, registration, source_id):
    entry = registration.get("plans", {}).get(name)
    common = {"source_id": str(source_id), "computed_at": registration.get("created_at")}
    if entry and (entry.get("transform") or entry.get("method") == "control_points"):
        return {"status": "placed", **entry, "frame": registration["frame"], **common}
    reason = registration.get("unplaced", {}).get(name, "Plan absent du calcul de placement.")
    return {"status": "unplaced", "reason": reason, **common}


async def load_plan_georeference(db_session, progress=None, task_id=None, log=print):
    if not PLAN_REGISTRATION_PATH.is_file():
        log("Placement des plans absent : lancer d’abord le calcul (plans historiques).")
        return
    registration = json.loads(PLAN_REGISTRATION_PATH.read_text(encoding="utf-8"))
    source_id = await get_data_source_id(db_session, SOURCE)
    run = await start_ingestion_run(db_session, SOURCE)
    updated = placed = 0
    try:
        building = aliased(Location)
        rows = (
            await db_session.execute(
                select(Location, building.code)
                .join(building, building.id == Location.parent_id)
                .where(Location.kind == "FLOOR", building.kind == "BUILDING")
            )
        ).all()
        for floor, building_code in rows:
            attributes = dict(floor.attributes or {})
            if "legacy_plan" not in attributes:
                continue
            value = placement(f"{building_code}_{floor.code}", registration, source_id)
            placed += value["status"] == "placed"
            # Only a changed placement counts as an update; the computation date alone does not.
            previous = {k: v for k, v in (attributes.get(SOURCE) or {}).items() if k != "computed_at"}
            if previous != {k: v for k, v in value.items() if k != "computed_at"}:
                updated += 1
            attributes[SOURCE] = value
            floor.attributes = attributes
        await db_session.flush()
        await finish_ingestion_run(db_session, run, updated=updated)
    except Exception as error:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(error)[:2000])
        raise
    log(f"Plans historiques : {placed} étages placés, {updated} placements modifiés.")


async def run():
    from db.seed import seed_default_data

    async with AsyncSessionLocal() as session:
        await seed_default_data(session)
        await load_plan_georeference(session)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(run())
