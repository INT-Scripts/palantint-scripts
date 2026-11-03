"""Merge Wemap locations without replacing housing, research or calibrations."""

import asyncio
import json
import re

from sqlalchemy import select

from core.map_reference import curate_collection
from core.places import place_category
from db.database import AsyncSessionLocal
from db.models import Event, Location, Organization, PersonHousing
from palantint_scripts.config import SCRAPS_MANUAL_DIR, WEMAP_DIR
from palantint_scripts.db_helpers import (
    finish_ingestion_run,
    get_data_source_id,
    start_ingestion_run,
)


def building_code(properties):
    name = properties.get("name", "")
    residences = sorted(set(re.findall(r"\bU[1-7]\b", name.upper())))
    if len(residences) == 1:
        return residences[0]
    if name.upper() == "FOYER":
        return "Foyer"
    return f"WEMAP-{properties['building_id']}"


def attach_feature(location, feature):
    attributes = dict(location.attributes or {})
    imported = dict(attributes.get("wemap", {}))
    imported["active"] = True
    if feature["properties"].get("curation"):
        imported["curation"] = feature["properties"]["curation"]
        imported["aliases"] = feature["properties"]["curation"]["aliases"]
    imported.setdefault("feature_ids", [])
    imported["feature_ids"] = sorted(set(imported["feature_ids"] + [feature["id"]]))
    # Keep a room's polygon as its primary geometry, rather than its POI centre.
    if "Polygon" in feature["geometry"]["type"] or not imported.get("geometry"):
        imported.update(
            {
                "geometry": feature["geometry"],
                "primary_feature_id": feature["id"],
                "properties": feature["properties"],
                "levels": feature["properties"].get("levels", []),
            }
        )
    attributes["wemap"] = imported
    location.attributes = attributes


def room_code(name):
    # A capacity annotation is not part of ADE's room identifier. Keep the
    # complete original label in the Wemap properties and Location.name.
    return re.sub(
        r"\s+(?:(?:SC|SI|SEC)\s*)?\d+\s*pl(?:aces)?\b.*$", "", name, flags=re.IGNORECASE
    ).strip()


async def load_wemap(db_session, progress=None, task_id=None, log=print):
    path = WEMAP_DIR / "campus.geojson"
    if not path.exists():
        log("Plans Wemap absents : lancer d’abord l’extraction.")
        return
    collection = json.loads(path.read_text(encoding="utf-8"))
    overrides = SCRAPS_MANUAL_DIR / "metadata" / "wemap-curations.json"
    extra_rules = (
        json.loads(overrides.read_text(encoding="utf-8")) if overrides.is_file() else []
    )
    collection = curate_collection(collection, extra_rules)
    features = collection.get("features", [])
    for feature in features:
        feature["properties"]["category"] = place_category(feature["properties"])
    if collection.get("type") != "FeatureCollection" or not any(
        f.get("properties", {}).get("kind") == "room" for f in features
    ):
        raise ValueError("Le fichier Wemap ne contient pas un plan complet de salles")
    for feature in features:
        curation = feature["properties"].get("curation")
        if curation:
            curation["source_id"] = str(
                await get_data_source_id(db_session, curation["source_code"])
            )
    source_id = await get_data_source_id(db_session, "wemap")
    run = await start_ingestion_run(db_session, "wemap")
    existing = list((await db_session.execute(select(Location))).scalars())
    by_feature = {
        feature_id: location
        for location in existing
        for feature_id in (location.attributes or {})
        .get("wemap", {})
        .get("feature_ids", [])
    }
    seen = set()
    created = updated = deactivated = 0

    async def upsert(feature, kind, code, parent=None, aliases=()):
        nonlocal created, updated
        location = by_feature.get(feature["id"])
        curation = feature["properties"].get("curation")
        if curation:
            match = curation["location"]
            candidates = [
                row
                for row in existing
                if row.kind == match["kind"]
                and row.name == match["name"]
                and row.is_active
            ]
            if len(candidates) > 1:
                raise ValueError(f"Curated location is ambiguous: {match['name']}")
            if candidates:
                canonical = candidates[0]
                if location and location.id != canonical.id:
                    attributes = dict(location.attributes or {})
                    geography = dict(attributes.get("wemap", {}))
                    geography["feature_ids"] = [
                        identity
                        for identity in geography.get("feature_ids", [])
                        if identity != feature["id"]
                    ]
                    if not geography["feature_ids"]:
                        geography["active"] = False
                    attributes["wemap"] = geography
                    location.attributes = attributes
                location = canonical
        if location is None:
            candidates = [
                row
                for row in existing
                if row.kind == kind
                and row.code.casefold()
                in {str(value).casefold() for value in (code, *aliases)}
                and (
                    row.parent_id == (parent.id if parent else None)
                    or row.parent_id is None
                    or kind == "APARTMENT"
                )
                and not (row.attributes or {}).get("wemap", {}).get("feature_ids")
            ]
            if len(candidates) == 1:
                location = candidates[0]
        if location is None:
            if any(
                row.kind == kind
                and row.code == code
                and row.parent_id == (parent.id if parent else None)
                for row in existing
            ):
                code = (
                    f"{code} [Wemap {feature['properties'].get('pk', feature['id'])}]"
                )
            location = Location(
                kind=kind,
                code=code,
                name=feature["properties"].get("name") or code,
                parent_id=parent.id if parent else None,
                source_id=source_id,
            )
            db_session.add(location)
            await db_session.flush()
            existing.append(location)
            created += 1
        else:
            updated += 1
            if location.source_id == source_id:
                location.is_active = True
                location.name = feature["properties"].get("name") or location.name
            if location.parent_id is None and parent:
                location.parent_id = parent.id
        attach_feature(location, feature)
        seen.add(location.id)
        by_feature[feature["id"]] = location
        return location

    try:
        buildings = {}
        for feature in features:
            properties = feature["properties"]
            if properties.get("kind") == "building":
                code = building_code(properties)
                buildings[str(properties["building_id"])] = await upsert(
                    feature, "BUILDING", code
                )

        floors = {}
        # Some floors have no explicit floor-outline feature, but their rooms
        # still carry a level. Create those nodes from the source level data.
        for feature in features:
            properties = feature["properties"]
            building = buildings.get(str(properties.get("building_id")))
            if not building:
                continue
            for level in properties.get("levels", []):
                key = (str(properties["building_id"]), str(float(level)))
                if key not in floors:
                    code = f"{float(level):g}"
                    floor = next(
                        (
                            row
                            for row in existing
                            if row.kind == "FLOOR"
                            and row.parent_id == building.id
                            and row.code == code
                        ),
                        None,
                    )
                    if not floor:
                        floor = Location(
                            kind="FLOOR",
                            code=code,
                            name=f"Étage {code}",
                            parent_id=building.id,
                            source_id=source_id,
                        )
                        db_session.add(floor)
                        await db_session.flush()
                        existing.append(floor)
                        created += 1
                    floors[key] = floor
                    seen.add(floor.id)
                if (
                    properties.get("kind") == "floor"
                    and "Polygon" in feature["geometry"]["type"]
                ):
                    attach_feature(floors[key], feature)

        # Prefer polygons over labels; link equivalent representations by PK.
        by_pk = {}
        ordered = sorted(
            features,
            key=lambda feature: 0 if "Polygon" in feature["geometry"]["type"] else 1,
        )
        for feature in ordered:
            properties = feature["properties"]
            kind = properties["kind"]
            if kind in ("building", "floor", "structure"):
                continue
            building = buildings.get(str(properties.get("building_id")))
            levels = properties.get("levels", [])
            floor = (
                floors.get((str(properties.get("building_id")), str(float(levels[0]))))
                if levels
                else None
            )
            pk = properties.get("curation", {}).get("id") or str(
                properties.get("pk", feature["id"])
            )
            if pk in by_pk:
                location = by_pk[pk]
                await upsert(
                    feature, location.kind, location.code, aliases=(location.name,)
                )
                continue
            name = str(properties.get("name") or "").strip()
            if kind == "poi" and not name:
                continue
            if kind not in ("room", "poi", "stairs", "elevator", "corridor"):
                continue
            # Bare apartment numbers resolve to the existing Maisel location.
            # Preserve its direct BUILDING parent: existing housing APIs rely on it.
            apartment = (
                next(
                    (
                        row
                        for row in existing
                        if row.kind == "APARTMENT" and row.code == name
                    ),
                    None,
                )
                if name
                else None
            )
            residential = building and (
                building.code.startswith("U") or building.code == "WEMAP-1365"
            )
            location_kind = (
                "APARTMENT"
                if apartment or (residential and re.fullmatch(r"\d{4}", name))
                else ("ROOM" if kind == "room" and not residential else "COMMON_AREA")
            )
            code = (
                room_code(name) if location_kind == "ROOM" else name
            ) or f"WEMAP-{properties.get('pk', feature['id'])}"
            parent = building if location_kind == "APARTMENT" else (floor or building)
            location = await upsert(
                feature, location_kind, code, parent, aliases=(name,)
            )
            by_pk[pk] = location

        referenced = set(
            (
                await db_session.execute(
                    select(Event.location_id).where(Event.location_id.is_not(None))
                )
            ).scalars()
        )
        referenced.update(
            (await db_session.execute(select(PersonHousing.location_id))).scalars()
        )
        referenced.update(
            (
                await db_session.execute(
                    select(Organization.location_id).where(
                        Organization.location_id.is_not(None)
                    )
                )
            ).scalars()
        )
        for location in existing:
            if (location.attributes or {}).get("wemap", {}).get(
                "active", False
            ) and location.id not in seen:
                attributes = dict(location.attributes or {})
                attributes["wemap"] = {**attributes.get("wemap", {}), "active": False}
                location.attributes = attributes
                # A removed map feature cannot remove a person's housing or
                # a room referenced by the agenda / a manually linked club.
                if (
                    location.source_id == source_id
                    and location.id not in referenced
                    and location.kind != "APARTMENT"
                ):
                    location.is_active = False
                deactivated += 1
        await finish_ingestion_run(
            db_session, run, created=created, updated=updated, deactivated=deactivated
        )
    except Exception as error:
        await finish_ingestion_run(
            db_session, run, status="FAILED", error=str(error)[:2000]
        )
        raise
    log(f"Wemap : {created} lieux ajoutés, {updated} enrichis, {deactivated} archivés.")


async def run():
    from db.seed import seed_default_data

    async with AsyncSessionLocal() as session:
        await seed_default_data(session)
        await load_wemap(session)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(run())
