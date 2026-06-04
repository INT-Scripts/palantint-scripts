import asyncio
import json
import os
import re
import unidecode
from typing import Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert

from db.database import AsyncSessionLocal
from db.models import Student, ClassGroup, StudentClassGroup
from palantint_scripts.utils import normalize_name

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))

# (normalize_name imported above)

async def load_groupes(db_session: AsyncSession, progress=None, task_id=None, log=print):
    """
    Ingests group topologies into the database, linking students to their academic classes.
    """
    json_path = os.path.join(DATA_DIR, "groupes.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Groups: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        groupes_data = json.load(f)

    if not groupes_data:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Groups: JSON empty. Skipped.[/yellow]", completed=1, total=1)
        return

    # 1. Map existing students for fuzzy matching
    log("Mapping students for identity matching...")
    result = await db_session.execute(select(Student))
    all_students = result.scalars().all()
    
    # Map normalized names to student objects
    student_map = {}
    for s in all_students:
        # Try multiple combinations to be safe
        n1 = normalize_name(f"{s.first_name}{s.last_name}")
        n2 = normalize_name(f"{s.last_name}{s.first_name}")
        if n1: student_map[n1] = s
        if n2: student_map[n2] = s

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Groups: Syncing {len(groupes_data)} groups...[/blue]", total=len(groupes_data), completed=0)

    # 2. Expand hierarchies and associate memberships
    log("Expanding group hierarchies and aggregating memberships...")
    club_memberships: Dict[str, set] = {} # normalized_club_name -> set(student_ids)
    inferred_display_names: Dict[str, str] = {} # normalized -> original

    for group_info in groupes_data:
        gname = group_info["name"]
        gmembers = {student_map[normalize_name(m["name"])].id for m in group_info.get("members", []) if normalize_name(m["name"]) in student_map}
        
        # Add to direct group
        norm_gname = normalize_name(gname)
        if norm_gname not in club_memberships: club_memberships[norm_gname] = set()
        club_memberships[norm_gname].update(gmembers)

        # Infer Parent Groups (e.g., Gp-EI1-G2a -> Gp-EI1-G2, Gp-EI1-G, Gp-EI1)
        # Regex to find scientific/academic group patterns
        m = re.match(r"(Gp-[A-Z]+[0-9])(-[A-Za-z0-9]+)?", gname)
        if m:
            base = m.group(1) # e.g., Gp-EI1
            suffix = m.group(2) or "" # e.g., -G2a
            
            # 1. Base Global Group (e.g., Gp-EI1)
            norm_base = normalize_name(base)
            if norm_base not in club_memberships: club_memberships[norm_base] = set()
            club_memberships[norm_base].update(gmembers)
            # Store display name for later
            if norm_base not in inferred_display_names: inferred_display_names[norm_base] = base
            
            # 2. Promo Global Group (e.g., Gp-EI1-G)
            promo_name = f"{base}-G"
            norm_promo = normalize_name(promo_name)
            if norm_promo not in club_memberships: club_memberships[norm_promo] = set()
            club_memberships[norm_promo].update(gmembers)
            if norm_promo not in inferred_display_names: inferred_display_names[norm_promo] = promo_name
            
            # 3. Level 2 Parent (e.g., Gp-EI1-G2) if current is Gp-EI1-G2a
            if len(suffix) >= 3: # e.g. -G2a
                sub_parent_name = f"{base}{suffix[:len(suffix)-1]}"
                norm_sub = normalize_name(sub_parent_name)
                if norm_sub not in club_memberships: club_memberships[norm_sub] = set()
                club_memberships[norm_sub].update(gmembers)
                if norm_sub not in inferred_display_names: inferred_display_names[norm_sub] = sub_parent_name

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Groups: Syncing {len(club_memberships)} hierarchy levels...[/blue]", total=len(club_memberships), completed=0)

    # 3. Promo-based Global Fallback (for students missing from any Gp- group)
    # This ensures "Base Curriculum" visibility for everyone.
    log("Applying promo-based global group fallbacks...")
    promo_mapping = {
        ("Télécom SudParis", "Ingénieur 1ère année"): "Gp-EI1",
        ("Télécom SudParis", "Ingénieur 2ème année"): "Gp-EI2",
        ("Télécom SudParis", "Ingénieur 3ème année"): "Gp-EI3",
        ("IMT-BS", "Management 1ère année"): "Gp-EM1",
        ("IMT-BS", "Management 2ème année"): "Gp-EM2",
        ("IMT-BS", "Management 3ème année"): "Gp-EM3",
    }
    
    # Track which students already have an academic group
    students_with_groups = set()
    for norm_name, sids in club_memberships.items():
        if norm_name.startswith("gp"):
            students_with_groups.update(sids)
            
    # Add remaining students to their base promo group
    for s in all_students:
        if s.id in students_with_groups: continue
        
        # Simple heuristic mapping
        target_gp = promo_mapping.get((s.ecole, s.promo))
        if not target_gp:
            # Try fuzzy matching on promo string
            if s.ecole == "Télécom SudParis":
                if "1ère" in str(s.promo): target_gp = "Gp-EI1"
                elif "2ème" in str(s.promo): target_gp = "Gp-EI2"
                elif "3ème" in str(s.promo): target_gp = "Gp-EI3"
            elif s.ecole == "IMT-BS":
                if "1ère" in str(s.promo): target_gp = "Gp-EM1"
                elif "2ème" in str(s.promo): target_gp = "Gp-EM2"
                elif "3ème" in str(s.promo): target_gp = "Gp-EM3"
        
        if target_gp:
            norm_tgp = normalize_name(target_gp)
            if norm_tgp not in club_memberships: club_memberships[norm_tgp] = set()
            club_memberships[norm_tgp].add(s.id)
            if norm_tgp not in inferred_display_names: inferred_display_names[norm_tgp] = target_gp
            # Also add to -G (Promo) variant for CMs
            promo_g = f"{target_gp}-G"
            norm_pg = normalize_name(promo_g)
            if norm_pg not in club_memberships: club_memberships[norm_pg] = set()
            club_memberships[norm_pg].add(s.id)
            if norm_pg not in inferred_display_names: inferred_display_names[norm_pg] = promo_g

    # 4. Create Clubs and Memberships
    for norm_name, student_ids in club_memberships.items():
        # Find original name from inferred map, groupes_data or reconstruct it
        display_name = inferred_display_names.get(norm_name)
        if not display_name:
            for g in groupes_data:
                if normalize_name(g["name"]) == norm_name:
                    display_name = g["name"]
                    break
        
        if not display_name:
            display_name = norm_name.upper()
        
        # Upsert ClassGroup
        stmt = insert(ClassGroup).values(name=display_name)
        upsert_stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"name": display_name}).returning(ClassGroup.id)
        result = await db_session.execute(upsert_stmt)
        class_group_id = result.scalar_one()

        # Sync Members
        await db_session.execute(delete(StudentClassGroup).where(StudentClassGroup.class_group_id == class_group_id))
        if student_ids:
            new_memberships = [
                {"student_id": sid, "class_group_id": class_group_id, "role": "Membre"}
                for sid in student_ids
            ]
            await db_session.execute(insert(StudentClassGroup), new_memberships)
        
        log(f"Synced [magenta]{display_name}[/magenta]: {len(student_ids)} members (Hierarchy Level).")
        if progress and task_id: progress.update(task_id, advance=1)

    await db_session.flush()
    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Groups: Done ({len(groupes_data)} groups).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_groupes(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
