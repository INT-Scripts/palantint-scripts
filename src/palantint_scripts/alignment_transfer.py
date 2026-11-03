"""Import a narrow, portable alignment export without touching research data."""

import argparse
import asyncio
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from api.private.maps import (
    MapMetadataSchema,
    ThreeDConfigSchema,
    _get_floor_location,
    get_3d_config,
    get_map_metadata,
)
from db.models import MapMetadata, ThreeDConfig
from palantint_scripts.config import EXPORTS_DIR


def validate_bundle(data):
    if data.get("schema_version") != 1 or not isinstance(data.get("maps"), list):
        raise ValueError("Format d’export d’alignements invalide")
    config = ThreeDConfigSchema.model_validate(data["config"])
    if not config.markers or len({m.id for m in config.markers}) != len(config.markers):
        raise ValueError("Bâtiments absents ou identifiants dupliqués")
    for marker in config.markers:
        transform = marker.wireframe
        numbers = [
            *transform.position,
            *transform.rotation,
            transform.scale,
            transform.floor_height,
            *(n for point in marker.footprint for n in point),
        ]
        if (
            not all(math.isfinite(n) for n in numbers)
            or transform.scale <= 0
            or transform.floor_height <= 0
        ):
            raise ValueError(f"Alignement invalide : {marker.id}")
    seen = set()
    for row in data["maps"]:
        key = (row["building_id"], row["floor_id"])
        if key in seen:
            raise ValueError("Calibration d’étage dupliquée")
        seen.add(key)
        metadata = MapMetadataSchema.model_validate(row)
        if len(metadata.pillars) not in (0, 2) or any(
            not math.isfinite(v) or not 0 <= v <= 100
            for p in metadata.pillars
            for v in (p.x, p.y)
        ):
            raise ValueError(f"Repères invalides : {key}")
    return config


async def import_alignments(db, data, backup_root=EXPORTS_DIR):
    config = validate_bundle(data)
    resolved = []
    for item in data["maps"]:
        floor = await _get_floor_location(
            db, item["building_id"], str(item["floor_id"])
        )
        if floor is None:
            raise ValueError(
                f"Étage inconnu : {item['building_id']} / {item['floor_id']}"
            )
        resolved.append((floor, item))
    previous = await get_3d_config(db=db, current_user=None)
    previous_maps = []
    for floor, item in resolved:
        metadata = await get_map_metadata(
            item["building_id"], str(item["floor_id"]), db=db, current_user=None
        )
        previous_maps.append(
            {**item, "pillars": [p.model_dump() for p in metadata.pillars]}
        )
    backup = Path(backup_root) / (
        "alignments-before-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    backup.mkdir(parents=True, exist_ok=False)
    (backup / "alignments.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "config": previous.model_dump(),
                "maps": previous_maps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # Imported identities update existing rows; unrelated local markers survive.
    markers = {m.id: m.model_dump(mode="json") for m in previous.markers}
    markers.update({m.id: m.model_dump(mode="json") for m in config.markers})
    row = (
        (await db.execute(select(ThreeDConfig).where(ThreeDConfig.key == "default")))
        .scalars()
        .first()
    )
    if row is None:
        row = ThreeDConfig(key="default")
        db.add(row)
    row.tile_mappings = {**previous.tile_mappings, **config.tile_mappings}
    row.markers = list(markers.values())
    for floor, item in resolved:
        metadata = (
            (
                await db.execute(
                    select(MapMetadata).where(MapMetadata.location_id == floor.id)
                )
            )
            .scalars()
            .first()
        )
        if metadata is None:
            metadata = MapMetadata(location_id=floor.id)
            db.add(metadata)
        metadata.pillars = item["pillars"]
    await db.flush()
    # This archive carries source provenance; the ordinary Vault exports the DB state.
    (backup / "imported.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (backup / "provenance.json").write_text(
        json.dumps(
            {
                "source": data.get("source"),
                "exported_at": data.get("exported_at"),
                "sha256": hashlib.sha256(
                    json.dumps(data, sort_keys=True).encode()
                ).hexdigest(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup


async def run(path):
    from db.database import AsyncSessionLocal

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    async with AsyncSessionLocal() as db:
        async with db.begin():
            backup = await import_alignments(db, data)
    print(f"Alignements importés. Sauvegarde : {backup}")


def main():
    parser = argparse.ArgumentParser(
        description="Importer les alignements 3D et calibrations exportés du portail"
    )
    parser.add_argument("path")
    asyncio.run(run(parser.parse_args().path))


if __name__ == "__main__":
    main()
