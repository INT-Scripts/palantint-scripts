import asyncio
import json
import os
import uuid
from datetime import datetime
from sqlalchemy.future import select
from unidecode import unidecode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from db.database import AsyncSessionLocal
from db.models import AgendaEvent, Student, StudentAgendaEvent

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps/agenda"))

def normalize_name(name_str):
    if not name_str: return ""
    n = unidecode(name_str).upper()
    n = n.replace("-", " ").replace("'", " ")
    return " ".join(n.split())

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

    cals_to_load = [cal_id for cal_id, info in index_data.items() if info.get("status") == "hydrated"]
    
    if not cals_to_load:
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: No hydrated calendars found in index.[/yellow]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Agenda: Loading {len(cals_to_load)} calendars...[/blue]", total=len(cals_to_load), completed=0)

    student_lookup = await get_student_lookup(db_session)
    
    total_events_loaded = 0

    for cal_id in cals_to_load:
        json_path = os.path.join(DATA_DIR, f"{cal_id}.json")
        if not os.path.exists(json_path):
            continue

        with open(json_path, "r", encoding="utf-8") as f:
            events_data = json.load(f)

        log(f"Syncing calendar [magenta]{cal_id}[/magenta] to DB...")
        
        for evt_data in events_data:
            stmt = select(AgendaEvent).filter_by(event_ref_id=evt_data["id"])
            result = await db_session.execute(stmt)
            existing_evt = result.scalars().first()
            
            try:
                start_dt = datetime.strptime(f"{evt_data['date']} {evt_data['start_time']}", "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(f"{evt_data['date']} {evt_data['end_time']}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue # Skip badly formatted dates
                
            professors = ", ".join(evt_data.get("professors", [])) if evt_data.get("professors") else ""
            
            if not existing_evt:
                new_evt = AgendaEvent(
                    id=uuid.uuid4(),
                    event_ref_id=evt_data["id"],
                    calendar_id=evt_data["calendar_id"],
                    name=evt_data["name"],
                    type=evt_data["type"],
                    start_time=start_dt,
                    end_time=end_dt,
                    room=evt_data.get("room"),
                    professors=professors,
                )
                db_session.add(new_evt)
                await db_session.flush()
                existing_evt = new_evt
            else:
                existing_evt.name = evt_data["name"]
                existing_evt.type = evt_data["type"]
                existing_evt.start_time = start_dt
                existing_evt.end_time = end_dt
                existing_evt.room = evt_data.get("room")
                existing_evt.professors = professors
                
            students = evt_data.get("students", [])
            if students:
                await db_session.execute(delete(StudentAgendaEvent).where(StudentAgendaEvent.event_id == existing_evt.id))
                matched_ids = set()
                for name in students:
                    norm = normalize_name(name)
                    s_id = student_lookup.get(norm) or student_lookup.get(norm.replace(" ", ""))
                    if s_id: matched_ids.add(s_id)
                for s_id in matched_ids:
                    db_session.add(StudentAgendaEvent(student_id=s_id, event_id=existing_evt.id))
            
            total_events_loaded += 1

        if progress and task_id:
            progress.update(task_id, advance=1)

    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Agenda: Done ({total_events_loaded} events across {len(cals_to_load)} cals).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_agenda(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
