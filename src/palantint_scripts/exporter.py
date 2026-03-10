import os
import json
import uuid
from typing import Callable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from db.models import Student, Club, AgendaEvent, MapMetadata, StudentRelationship, SocialLink, Media, StudentClub, ClubLink

async def export_db_data(db_session: AsyncSession, log: Callable = print):
    """
    PalantINT Vault Exporter
    -----------------------
    Exports the current state of the database to data/exports/.
    This includes both automated subject data AND manual OSINT intelligence.
    """
    export_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data/exports"))
    os.makedirs(export_dir, exist_ok=True)
    
    # 1. CORE SUBJECTS & INFRASTRUCTURE (Full Snapshots)
    # These tables are overwritten on every export to reflect the current DB state.
    export_targets = [
        {"model": Student, "filename": "students.json", "desc": "Subject Registry"},
        {"model": Club, "filename": "clubs.json", "desc": "Organization Registry"},
        {"model": ClubLink, "filename": "club_links.json", "desc": "Organization Links"},
        {"model": StudentClub, "filename": "memberships.json", "desc": "Subject-Organization Ties"},
        {"model": MapMetadata, "filename": "maps.json", "desc": "Map Calibration Vault"},
        {"model": StudentRelationship, "filename": "relationships.json", "desc": "Social Graph Vault"},
        {"model": SocialLink, "filename": "socials.json", "desc": "External Handles Vault"},
        {"model": Media, "filename": "media.json", "desc": "Comms Log & Media Metadata"},
    ]
    
    for target in export_targets:
        model = target["model"]
        name = model.__tablename__
        filename = target["filename"]
        
        log(f"Archiving [magenta]{target['desc']}[/magenta] ({name})...")
        
        result = await db_session.execute(select(model))
        items = result.scalars().all()
        
        data = []
        for item in items:
            record = {}
            # Use SQLModel/SQLAlchemy inspection to get all columns
            for col in item.__table__.columns:
                val = getattr(item, col.name)
                # Ensure JSON-safe types (UUIDs and Datetimes to strings)
                if isinstance(val, uuid.UUID):
                    val = str(val)
                elif hasattr(val, "isoformat"):
                    val = val.isoformat()
                record[col.name] = val
            data.append(record)
            
        out_path = os.path.join(export_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            
        log(f"[green]✓ Exported {len(data)} records to {filename}[/green]")

    # 2. PRECISION MAPPINGS (Formatted for specialized loaders)
    log("Generating [magenta]Precision Apartment Map[/magenta]...")
    result = await db_session.execute(
        select(Student.trombint_id, Student.apartment)
        .where(Student.apartment.isnot(None))
    )
    
    # Standardized format: { "trombint_id": "apartment_number" }
    mapping = {row.trombint_id: row.apartment for row in result.all() if row.trombint_id and row.apartment}
            
    with open(os.path.join(export_dir, "apartments.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=4, ensure_ascii=False)
    
    log(f"[green]✓ Exported {len(mapping)} mappings to apartments.json[/green]")
    log(f"\n[bold green]Vault Snapshot Complete.[/bold green] All OSINT data is now portable.")
