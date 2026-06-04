import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from sqlalchemy.future import select
from unidecode import unidecode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from db.database import AsyncSessionLocal
from db.models import AgendaEvent, Student, StudentAgendaEvent, Club, ClassGroup, EventClassGroup
from palantint_scripts.utils import normalize_name

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps/agenda"))

async def get_student_lookup(session: AsyncSession):
    stmt = select(Student)
    result = await session.execute(stmt)
    students = result.scalars().all()
    lookup = {}
    for s in students:
        first = normalize_name(s.first_name)
        last = normalize_name(s.last_name)
        lookup[f"{last} {first}"] = s.id
        lookup[f"{first} {last}"] = s.id
        lookup[f"{last}{first}"] = s.id
        lookup[f"{first}{last}"] = s.id
    return lookup

async def load_agenda(db_session: AsyncSession, progress=None, task_id=None, log=print):
    index_path = os.path.join(DATA_DIR, "index.json")
    if not os.path.exists(index_path):
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: index.json not found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    cals_to_load = [cal_id for cal_id, info in index_data.items() if info.get("event_count", 0) > 0]
    
    if not cals_to_load:
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: No events found in index.[/yellow]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Agenda: Processing {len(cals_to_load)} agendas...[/blue]", total=len(cals_to_load), completed=0)

    log("Mapping students and academic groups for identity resolution...")
    student_lookup = await get_student_lookup(db_session)
    
    # Load groupes.json to resolve student external ID (id_objet) -> student UUID
    groupes_path = os.path.abspath(os.path.join(DATA_DIR, "../groupes.json"))
    id_objet_to_student_id = {}
    if os.path.exists(groupes_path):
        try:
            with open(groupes_path, "r", encoding="utf-8") as f:
                groupes_data = json.load(f)
            for grp in groupes_data:
                for member in grp.get("members", []):
                    obj_id = member.get("id_objet")
                    m_name = member.get("name")
                    if obj_id and m_name:
                        norm_name = normalize_name(m_name)
                        if norm_name in student_lookup:
                            id_objet_to_student_id[str(obj_id)] = student_lookup[norm_name]
        except Exception as e:
            log(f"Warning: Failed to parse groupes.json student mappings: {e}")
    
    result = await db_session.execute(select(Club))
    all_clubs = result.scalars().all()
    club_map = {normalize_name(c.name): c.id for c in all_clubs}
    
    result_cg = await db_session.execute(select(ClassGroup))
    all_class_groups = result_cg.scalars().all()
    class_group_map = {normalize_name(cg.name): cg.id for cg in all_class_groups}
    
    for c in all_clubs:
        if c.slug: club_map[c.slug] = c.id
    
    total_events_loaded = 0

    for cal_id in cals_to_load:
        cal_info = index_data[cal_id]
        cal_name = cal_info.get("name", "")
        json_path = os.path.join(DATA_DIR, f"{cal_id}.json")
        if not os.path.exists(json_path): continue

        with open(json_path, "r", encoding="utf-8") as f:
            events_data = json.load(f)

        log(f"Merging [magenta]{cal_name}[/magenta] ([cyan]{len(events_data)} events[/cyan])...")
        
        # --- Context Inference from Index Name ---
        # School: TSP/Ingénieur/LSH -> EI, Management/EM/Bachelor/PGE -> EM
        school = ""
        if any(w in cal_name for w in ["TSP", "Ingénieur", "LSH", "Telecom"]): school = "EI"
        elif any(w in cal_name for w in ["EM", "Management", "Bachelor", "BACH", "PGE"]): school = "EM"
        
        # Year: 1/2/3
        ctx = ""
        if any(w in cal_name for w in ["1er", "1ère", "1A", "1st"]): ctx = "1"
        elif any(w in cal_name for w in ["2ème", "2A", "2nd"]): ctx = "2"
        elif any(w in cal_name for w in ["3ème", "3A", "3rd"]): ctx = "3"
        # Fallback to alphanumeric codes (EM1, EI2, etc.)
        if not ctx:
            m_year = re.search(r"(EM|EI|BACH|FISA|FMSC)([1-3])", cal_name, re.I)
            if m_year: ctx = m_year.group(2)
            
        is_student_cal = cal_id.startswith("USR")
        student_id = None
        if is_student_cal:
            obj_id = cal_id[3:]
            student_id = id_objet_to_student_id.get(obj_id)
        
        for evt_data in events_data:
            stmt = select(AgendaEvent).filter_by(event_ref_id=str(evt_data["id"]))
            result = await db_session.execute(stmt)
            existing_evt = result.scalars().first()
            
            try:
                t_start = evt_data['start_time'].replace('h', ':')
                t_end = evt_data['end_time'].replace('h', ':')
                start_dt = datetime.strptime(f"{evt_data['date']} {t_start}", "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{evt_data['date']} {t_end}", "%Y-%m-%d %H:%M")
            except Exception: continue
                
            professors = ", ".join(evt_data.get("professors", [])) if evt_data.get("professors") else ""
            club_id = None
            matched_class_group_ids = set()
            
            # --- Link to Club (Group Topology) & ClassGroup ---
            groups = evt_data.get("groups") or []
            if isinstance(groups, str): groups = [groups]
            
            if not is_student_cal:
                # 1. Direct Matching from JSON Groups
                for grp_name in groups:
                    if not grp_name: continue
                    norm_grp = normalize_name(grp_name)
                    # Try direct match or prefix match (sometimes JSON omits Gp-)
                    candidates = [norm_grp]
                    if not norm_grp.startswith("gp"):
                        candidates.append(f"gp{norm_grp}")
                    
                    for cand in candidates:
                        if cand in class_group_map:
                            matched_class_group_ids.add(class_group_map[cand])
                        elif cand in club_map:
                            club_id = club_map[cand]
                            break

                # 2. Section Regex Extraction (Fuzzy name match)
                if school and ctx:
                    evt_name = evt_data.get("name", "")
                    # Matches "Gr 2a", "TD 4", "G2a", etc.
                    m = re.search(r"\b(Gr|TD|G)\s?([0-9]+[a-zA-Z]*)\b", evt_name, re.I)
                    if m:
                        short_id = m.group(2).lower()
                        candidates = [
                            f"gp{school.lower()}{ctx}{short_id}",
                            f"gp{school.lower()}{ctx}g{short_id}",
                            f"gp{school.lower()}{ctx}tdg{short_id}"
                        ]
                        for cand in candidates:
                            if cand in class_group_map:
                                matched_class_group_ids.add(class_group_map[cand])
                            elif cand in club_map:
                                club_id = club_map[cand]
                                break

                # 3. Fallback to Year-Wide Promotion Cohort
                # If it's a global calendar event and no specific subgroup (like Gr 2a) was matched or mentioned,
                # then it is implicitly for the entire promotion cohort (e.g. Gp-EI2-G).
                if not club_id and not matched_class_group_ids and school and ctx:
                    promo_grp_name = f"gp{school.lower()}{ctx}g"
                    if promo_grp_name in class_group_map:
                        matched_class_group_ids.add(class_group_map[promo_grp_name])
                    elif promo_grp_name in club_map:
                        club_id = club_map[promo_grp_name]

            # --- Upsert Event ---
            if not existing_evt:
                existing_evt = AgendaEvent(
                    id=uuid.uuid4(), event_ref_id=str(evt_data["id"]), calendar_id=cal_id,
                    name=evt_data["name"], type=evt_data["type"], start_time=start_dt, end_time=end_dt,
                    room=evt_data.get("room"), professors=professors, club_id=club_id
                )
                db_session.add(existing_evt)
            else:
                existing_evt.name = evt_data["name"]; existing_evt.type = evt_data["type"]
                existing_evt.start_time = start_dt; existing_evt.end_time = end_dt
                existing_evt.room = evt_data.get("room"); existing_evt.professors = professors
                # Only update club_id if it was previously Null and we found a match now
                if club_id and not existing_evt.club_id:
                    existing_evt.club_id = club_id
            
            await db_session.flush()

            if not is_student_cal:
                # --- Sync Class Group Links ---
                await db_session.execute(delete(EventClassGroup).where(EventClassGroup.event_id == existing_evt.id))
                if matched_class_group_ids:
                    new_links = [
                        EventClassGroup(event_id=existing_evt.id, class_group_id=cg_id)
                        for cg_id in matched_class_group_ids
                    ]
                    db_session.add_all(new_links)
            else:
                # --- Sync Student Links ---
                if student_id:
                    stmt_sae = select(StudentAgendaEvent).filter_by(event_id=existing_evt.id, student_id=student_id)
                    res_sae = await db_session.execute(stmt_sae)
                    if not res_sae.scalars().first():
                        sae_link = StudentAgendaEvent(event_id=existing_evt.id, student_id=student_id)
                        db_session.add(sae_link)
                
            await db_session.flush()
            total_events_loaded += 1
            if not club_id and not matched_class_group_ids:
                # Optional: Log failures to identify missing groups (limit to first 10 per calendar)
                pass 

        if progress and task_id: progress.update(task_id, advance=1)

    await db_session.flush()
    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Agenda: Done ({total_events_loaded} events).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_agenda(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
