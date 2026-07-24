import os
import json
import uuid
from typing import Callable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Student, MapMetadata, ThreeDConfig, StudentRelationship, SocialLink, Media, RelationshipType

async def export_db_data(db_session: AsyncSession, log: Callable = print):
    """
    PalantINT Vault Exporter
    -----------------------
    Exports human OSINT intelligence and admin infrastructure calibrations to data/exports/.
    Excludes automated web scrap data (scraped students, clubs, agendas) which are harvested by scrapers.
    """
    export_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data/exports"))
    os.makedirs(export_dir, exist_ok=True)

    # 1. MAP METADATA (Structural Calibration Vault)
    log("Archiving [magenta]Map Calibration Vault[/magenta] (maps_metadata)...")
    res_maps = await db_session.execute(select(MapMetadata))
    maps_data = []
    for m in res_maps.scalars().all():
        maps_data.append({
            "id": str(m.id),
            "building_id": m.building_id,
            "floor_id": m.floor_id,
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
    res_socials = await db_session.execute(
        select(SocialLink, Student.trombint_id)
        .join(Student, SocialLink.student_id == Student.id)
    )
    socials_data = []
    for social, trombint_id in res_socials.all():
        socials_data.append({
            "id": str(social.id),
            "student_id": str(social.student_id),
            "trombint_id": trombint_id,
            "platform": social.platform,
            "username": social.username,
            "url": social.url
        })
    with open(os.path.join(export_dir, "socials.json"), "w", encoding="utf-8") as f:
        json.dump(socials_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(socials_data)} social handles to socials.json[/green]")

    # 3. SOCIAL GRAPH / RELATIONSHIPS (Social Graph Vault)
    log("Archiving [magenta]Social Graph Vault[/magenta] (student_relationships)...")
    stmt_rel = (
        select(StudentRelationship, RelationshipType.name.label("type_name"))
        .join(RelationshipType, StudentRelationship.relationship_type_id == RelationshipType.id)
    )
    res_rel = await db_session.execute(stmt_rel)
    rel_rows = res_rel.all()

    relationships_data = []
    for rel, type_name in rel_rows:
        student_a = await db_session.get(Student, rel.student_a_id)
        student_b = await db_session.get(Student, rel.student_b_id)

        relationships_data.append({
            "id": str(rel.id),
            "student_a_id": str(rel.student_a_id),
            "student_a_trombint_id": student_a.trombint_id if student_a else None,
            "student_b_id": str(rel.student_b_id),
            "student_b_trombint_id": student_b.trombint_id if student_b else None,
            "relationship_type_id": str(rel.relationship_type_id),
            "relationship_type_name": type_name,
            "created_at": rel.created_at.isoformat() if hasattr(rel.created_at, "isoformat") else str(rel.created_at)
        })
    with open(os.path.join(export_dir, "relationships.json"), "w", encoding="utf-8") as f:
        json.dump(relationships_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(relationships_data)} relationships to relationships.json[/green]")

    # 4. COMMS LOG & MEDIA (Media Vault)
    log("Archiving [magenta]Comms Log & Media Vault[/magenta] (media)...")
    res_media = await db_session.execute(
        select(Media, Student.trombint_id)
        .join(Student, Media.student_id == Student.id)
    )
    media_data = []
    for m, trombint_id in res_media.all():
        media_data.append({
            "id": str(m.id),
            "student_id": str(m.student_id),
            "trombint_id": trombint_id,
            "type": m.type,
            "file_path": m.file_path,
            "content": m.content,
            "author_name": m.author_name,
            "uploaded_by_user_id": str(m.uploaded_by_user_id) if m.uploaded_by_user_id else None,
            "uploaded_at": m.uploaded_at.isoformat() if hasattr(m.uploaded_at, "isoformat") else str(m.uploaded_at)
        })
    with open(os.path.join(export_dir, "media.json"), "w", encoding="utf-8") as f:
        json.dump(media_data, f, indent=4, ensure_ascii=False)
    log(f"[green]✓ Exported {len(media_data)} media records to media.json[/green]")

    # 5. PRECISION HOUSING MAP (Student -> Apartment Vault)
    log("Archiving [magenta]Precision Housing Map[/magenta] (apartments.json)...")
    res_apts = await db_session.execute(
        select(Student.trombint_id, Student.apartment)
        .where(Student.apartment.isnot(None))
    )
    mapping = {row.trombint_id: row.apartment for row in res_apts.all() if row.trombint_id and row.apartment}
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
