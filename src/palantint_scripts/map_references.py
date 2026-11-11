"""Portable backup of manual plan metadata and Wemap curation inputs."""

import json
from pathlib import Path

from sqlalchemy import select

from db.models import Location
from palantint_scripts.config import PLAN_SPACES_PATH, SCRAPS_MANUAL_DIR
from palantint_scripts.db_helpers import get_data_source_id


async def export_references(db, export_dir):
    rows = (await db.execute(select(Location))).scalars().all()
    buildings = {row.id: row.code for row in rows if row.kind == "BUILDING"}
    plans = []
    for row in rows:
        if row.kind not in ("BUILDING", "FLOOR") or not (row.attributes or {}).get(
            "legacy_plan"
        ):
            continue
        plans.append(
            {
                "building": row.code
                if row.kind == "BUILDING"
                else buildings.get(row.parent_id),
                "floor": row.code if row.kind == "FLOOR" else None,
                "metadata": row.attributes["legacy_plan"],
            }
        )
    path = SCRAPS_MANUAL_DIR / "metadata" / "wemap-curations.json"
    curations = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    controls = SCRAPS_MANUAL_DIR / "metadata" / "plan-georeferencing.json"
    georeferencing = (
        json.loads(controls.read_text(encoding="utf-8")) if controls.is_file() else {}
    )
    plan_spaces = (
        json.loads(PLAN_SPACES_PATH.read_text(encoding="utf-8"))
        if PLAN_SPACES_PATH.is_file()
        else {}
    )
    Path(export_dir, "map_references.json").write_text(
        json.dumps(
            {
                "plans": plans,
                "curations": curations,
                "georeferencing": georeferencing,
                "plan_spaces": plan_spaces,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


async def restore_references(db, export_dir):
    path = Path(export_dir, "map_references.json")
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    source_id = await get_data_source_id(db, "vault_manual")
    for item in data.get("plans", []):
        if not item.get("building"):
            continue
        building = (
            (
                await db.execute(
                    select(Location).where(
                        Location.kind == "BUILDING", Location.code == item["building"]
                    )
                )
            )
            .scalars()
            .first()
        )
        if building is None:
            building = Location(
                kind="BUILDING",
                code=item["building"],
                name=item["building"],
                source_id=source_id,
            )
            db.add(building)
            await db.flush()
        target = building
        if item.get("floor") is not None:
            target = (
                (
                    await db.execute(
                        select(Location).where(
                            Location.kind == "FLOOR",
                            Location.parent_id == building.id,
                            Location.code == item["floor"],
                        )
                    )
                )
                .scalars()
                .first()
            )
            if target is None:
                target = Location(
                    kind="FLOOR",
                    code=item["floor"],
                    parent_id=building.id,
                    source_id=source_id,
                )
                db.add(target)
        target.attributes = {
            **(target.attributes or {}),
            "legacy_plan": item["metadata"],
            "legacy_plan_provenance": {
                "source_code": "vault_manual",
                "source_id": str(source_id),
            },
        }
    curations = SCRAPS_MANUAL_DIR / "metadata" / "wemap-curations.json"
    # Existing manual source files remain authoritative; a backup only fills
    # a missing source, never overwrites newer local curation edits.
    if data.get("curations") and not curations.exists():
        curations.parent.mkdir(parents=True, exist_ok=True)
        curations.write_text(
            json.dumps(data["curations"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    controls = SCRAPS_MANUAL_DIR / "metadata" / "plan-georeferencing.json"
    if data.get("georeferencing") and not controls.exists():
        controls.parent.mkdir(parents=True, exist_ok=True)
        controls.write_text(
            json.dumps(data["georeferencing"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if data.get("plan_spaces") and not PLAN_SPACES_PATH.exists():
        PLAN_SPACES_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAN_SPACES_PATH.write_text(
            json.dumps(data["plan_spaces"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    await db.flush()
