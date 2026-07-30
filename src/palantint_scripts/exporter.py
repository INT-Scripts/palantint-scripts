import os
import json
from typing import Callable, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import (
    DataSource,
    ExternalIdentity,
    Location,
    MapMetadata,
    Media,
    PersonHousing,
    PersonRelationship,
    RelationshipType,
    SocialLink,
    ThreeDConfig,
)

TROMBINT_SOURCE = "trombint"


async def _trombint_id_map(db_session: AsyncSession) -> dict:
    """person_id -> trombint_id, for embedding a human-readable identity
    alongside every exported row (mirrors the old flat Student.trombint_id
    convenience)."""
    result = await db_session.execute(
        select(ExternalIdentity.person_id, ExternalIdentity.external_id)
        .join(DataSource, DataSource.id == ExternalIdentity.source_id)
        .where(DataSource.code == TROMBINT_SOURCE)
    )
    return {str(person_id): external_id for person_id, external_id in result.all()}


async def export_db_data(db_session: AsyncSession, log: Callable = print, export_dir: Optional[str] = None):
    """
    PalantINT Vault Exporter
    -----------------------
    Exports human OSINT intelligence and admin infrastructure calibrations to data/exports/.
    Excludes automated web scrap data (scraped students, clubs, agendas) which are harvested by scrapers.

    `export_dir` defaults to the real project `data/exports/` directory; pass
    an override (e.g. a pytest `tmp_path`) for tests so they never touch the
    real exports used by the running app.
    """
    if export_dir is None:
        export_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data/exports"))
    os.makedirs(export_dir, exist_ok=True)

    trombint_ids = await _trombint_id_map(db_session)

    # 1. MAP METADATA (Structural Calibration Vault)
    log("Archiving [magenta]Map Calibration Vault[/magenta] (maps_metadata)...")
    res_maps = await db_session.execute(select(MapMetadata))
    maps_data = []
    for m in res_maps.scalars().all():
        floor = await db_session.get(Location, m.location_id)
        building = await db_session.get(Location, floor.parent_id) if floor and floor.parent_id else None
        maps_data.append({
            "id": str(m.id),
            "building_id": building.code if building else None,
            "floor_id": floor.code if floor else None,
            "pillars": m.pillars
        })
    with open(os.path.join(export_dir, "maps.json"), "w", encoding="utf-8") as f:
        json.dump(maps_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(maps_data)} map calibrations to maps.json[/green]")

    # 1b. 3D MAP CONFIG (Tile Mappings & Waypoints Vault)
    log("Archiving [magenta]3D Map Configuration Vault[/magenta] (three_d_config)...")
    res_3d = await db_session.execute(select(ThreeDConfig).where(ThreeDConfig.key == "default"))
    cfg_3d = res_3d.scalars().first()
    c3d_data = {
        "tile_mappings": cfg_3d.tile_mappings if cfg_3d else {},
        "markers": cfg_3d.markers if cfg_3d else []
    }
    with open(os.path.join(export_dir, "3d_config.json"), "w", encoding="utf-8") as f:
        json.dump(c3d_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported 3D map configuration to 3d_config.json[/green]")

    # 2. SOCIAL LINKS (External Handles Vault)
    log("Archiving [magenta]External Handles Vault[/magenta] (social_links)...")
    res_socials = await db_session.execute(select(SocialLink))
    socials_data = []
    for social in res_socials.scalars().all():
        socials_data.append({
            "id": str(social.id),
            "person_id": str(social.person_id),
            "trombint_id": trombint_ids.get(str(social.person_id)),
            "platform": social.platform,
            "username": social.username,
            "url": social.url,
            "confidence": social.confidence,
        })
    with open(os.path.join(export_dir, "socials.json"), "w", encoding="utf-8") as f:
        json.dump(socials_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(socials_data)} social handles to socials.json[/green]")

    # 3. SOCIAL GRAPH / RELATIONSHIPS (Social Graph Vault)
    log("Archiving [magenta]Social Graph Vault[/magenta] (person_relationships)...")
    stmt_rel = (
        select(PersonRelationship, RelationshipType.name.label("type_name"))
        .join(RelationshipType, PersonRelationship.relationship_type_id == RelationshipType.id)
    )
    res_rel = await db_session.execute(stmt_rel)
    rel_rows = res_rel.all()

    relationships_data = []
    for rel, type_name in rel_rows:
        relationships_data.append({
            "id": str(rel.id),
            "person_a_id": str(rel.person_a_id),
            "person_a_trombint_id": trombint_ids.get(str(rel.person_a_id)),
            "person_b_id": str(rel.person_b_id),
            "person_b_trombint_id": trombint_ids.get(str(rel.person_b_id)),
            "relationship_type_id": str(rel.relationship_type_id),
            "relationship_type_name": type_name,
            "confidence": rel.confidence,
            "evidence_media_id": str(rel.evidence_media_id) if rel.evidence_media_id else None,
            "created_at": rel.created_at.isoformat() if hasattr(rel.created_at, "isoformat") else str(rel.created_at)
        })
    with open(os.path.join(export_dir, "relationships.json"), "w", encoding="utf-8") as f:
        json.dump(relationships_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(relationships_data)} relationships to relationships.json[/green]")

    # 4. COMMS LOG & MEDIA (Media Vault)
    log("Archiving [magenta]Comms Log & Media Vault[/magenta] (media)...")
    res_media = await db_session.execute(select(Media))
    media_data = []
    for m in res_media.scalars().all():
        media_data.append({
            "id": str(m.id),
            "person_id": str(m.person_id),
            "trombint_id": trombint_ids.get(str(m.person_id)),
            "type": m.kind,
            "file_path": m.file_path,
            "content": m.content,
            "author_name": m.author_name,
            "uploaded_by_user_id": str(m.uploaded_by_user_id) if m.uploaded_by_user_id else None,
            "uploaded_at": m.uploaded_at.isoformat() if hasattr(m.uploaded_at, "isoformat") else str(m.uploaded_at)
        })
    with open(os.path.join(export_dir, "media.json"), "w", encoding="utf-8") as f:
        json.dump(media_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(media_data)} media records to media.json[/green]")

    # 5. PRECISION HOUSING MAP (Person -> Apartment Vault)
    log("Archiving [magenta]Precision Housing Map[/magenta] (apartments.json)...")
    res_housing = await db_session.execute(
        select(PersonHousing.person_id, Location.code)
        .join(Location, Location.id == PersonHousing.location_id)
        .where(PersonHousing.ended_at.is_(None))
    )
    mapping = {
        trombint_ids[str(person_id)]: apt_code
        for person_id, apt_code in res_housing.all()
        if str(person_id) in trombint_ids and apt_code
    }
    with open(os.path.join(export_dir, "apartments.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(mapping)} housing mappings to apartments.json[/green]")

    # Clean up obsolete scraped files from exports dir if present
    obsolete_files = ["students.json", "clubs.json", "club_links.json", "memberships.json", "maisel_apartments.json"]
    for obs in obsolete_files:
        obs_path = os.path.join(export_dir, obs)
        if os.path.exists(obs_path):
            try:
                os.remove(obs_path)
                log(f"[dim]Removed legacy scraped export file: {obs}[/dim]")
            except Exception:
                pass

    log(f"\n[bold green]Vault Snapshot Complete.[/bold green] OSINT & Infrastructure data saved.")
