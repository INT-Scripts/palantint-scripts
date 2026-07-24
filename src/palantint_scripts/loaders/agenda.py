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
from db.models import AgendaEvent, Club, ClassGroup, EventClassGroup
from palantint_scripts.utils import normalize_name

from palantint_scripts.config import SCRAPS_AUTO_DIR

DATA_DIR = str(SCRAPS_AUTO_DIR / "agenda")

async def load_agenda(db_session: AsyncSession, progress=None, task_id=None, log=print):
    index_path = os.path.join(DATA_DIR, "index.json")
    if not os.path.exists(index_path):
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: index.json not found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)

    # Only load group/project/resource calendars (exclude personal USR student calendars)
    cals_to_load = [
        cal_id for cal_id, info in index_data.items()
        if info.get("event_count", 0) > 0 and not cal_id.startswith("USR")
    ]
    
    if not cals_to_load:
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: No group events found in index.[/yellow]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Agenda: Processing {len(cals_to_load)} agendas...[/blue]", total=len(cals_to_load), completed=0)

    log("Resolving group topologies and clubs...")
    
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
                if club_id and not existing_evt.club_id:
                    existing_evt.club_id = club_id
            
            await db_session.flush()

            # --- Sync Class Group Links ---
            await db_session.execute(delete(EventClassGroup).where(EventClassGroup.event_id == existing_evt.id))
            if matched_class_group_ids:
                new_links = [
                    EventClassGroup(event_id=existing_evt.id, class_group_id=cg_id)
                    for cg_id in matched_class_group_ids
                ]
                db_session.add_all(new_links)
                
            await db_session.flush()
            total_events_loaded += 1

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
