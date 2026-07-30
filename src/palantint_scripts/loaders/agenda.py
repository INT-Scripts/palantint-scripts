import asyncio
import json
import os
import re
import uuid
from datetime import datetime
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from db.database import AsyncSessionLocal
from db.models import Event, EventOrganization, EventPresenter, Location, Organization, Person
from palantint_scripts.utils import normalize_name

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import finish_ingestion_run, get_data_source_id, start_ingestion_run

DATA_DIR = str(SCRAPS_AUTO_DIR / "agenda")

SOURCE_CODE = "agenda_ade"


def _kind_for(raw_type: str) -> str:
    t = (raw_type or "").lower()
    if "exam" in t or "partiel" in t:
        return "EXAM"
    return "COURSE"


async def _get_or_create_room(db_session: AsyncSession, room_name: str) -> Location:
    result = await db_session.execute(select(Location).where(Location.kind == "ROOM", Location.code == room_name))
    room = result.scalars().first()
    if not room:
        room = Location(kind="ROOM", code=room_name, name=room_name)
        db_session.add(room)
        await db_session.flush()
    return room


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

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    source_id = await get_data_source_id(db_session, SOURCE_CODE)

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Load Agenda: Processing {len(cals_to_load)} agendas...[/blue]", total=len(cals_to_load), completed=0)

    log("Resolving group topologies, clubs and professors...")

    result = await db_session.execute(select(Organization).where(Organization.kind.in_(["CLUB", "BUREAU"])))
    all_clubs = result.scalars().all()
    club_map = {normalize_name(c.name): c.id for c in all_clubs}
    for c in all_clubs:
        if c.slug: club_map[normalize_name(c.slug)] = c.id

    result_cg = await db_session.execute(select(Organization).where(Organization.kind == "CLASS_GROUP"))
    all_class_groups = result_cg.scalars().all()
    class_group_map = {normalize_name(cg.name): cg.id for cg in all_class_groups}

    result_prof = await db_session.execute(select(Person).where(Person.kind == "PROFESSOR"))
    all_professors = result_prof.scalars().all()
    professor_map = {normalize_name(f"{p.first_name}{p.last_name}"): p.id for p in all_professors}

    total_events_loaded = 0

    try:
        for cal_id in cals_to_load:
            cal_info = index_data[cal_id]
            cal_name = cal_info.get("name", "")
            json_path = os.path.join(DATA_DIR, f"{cal_id}.json")
            if not os.path.exists(json_path): continue

            with open(json_path, "r", encoding="utf-8") as f:
                events_data = json.load(f)

            log(f"Merging [magenta]{cal_name}[/magenta] ([cyan]{len(events_data)} events[/cyan])...")

            for evt_data in events_data:
                stmt = select(Event).filter_by(external_ref=str(evt_data["id"]))
                result = await db_session.execute(stmt)
                existing_evt = result.scalars().first()

                try:
                    t_start = evt_data['start_time'].replace('h', ':')
                    t_end = evt_data['end_time'].replace('h', ':')
                    start_dt = datetime.strptime(f"{evt_data['date']} {t_start}", "%Y-%m-%d %H:%M")
                    end_dt = datetime.strptime(f"{evt_data['date']} {t_end}", "%Y-%m-%d %H:%M")
                except Exception: continue

                professor_names = evt_data.get("professors", []) or []
                presenters_raw = ", ".join(professor_names) if professor_names else None
                matched_professor_ids = {
                    professor_map[normalize_name(name)]
                    for name in professor_names
                    if normalize_name(name) in professor_map
                }

                room_name = evt_data.get("room")
                room = await _get_or_create_room(db_session, room_name) if room_name else None

                club_org_id = None
                matched_class_group_ids = set()

                # --- Link to Club (Group Topology) & ClassGroup ---
                groups = evt_data.get("groups") or []
                if isinstance(groups, str): groups = [groups]

                for grp_name in groups:
                    if not grp_name: continue
                    norm_grp = normalize_name(grp_name)
                    candidates = [norm_grp]
                    if not norm_grp.startswith("gp"):
                        candidates.append(f"gp{norm_grp}")

                    for cand in candidates:
                        if cand in class_group_map:
                            matched_class_group_ids.add(class_group_map[cand])
                        elif cand in club_map:
                            club_org_id = club_map[cand]
                            break

                # --- Upsert Event ---
                if not existing_evt:
                    existing_evt = Event(
                        id=uuid.uuid4(), external_ref=str(evt_data["id"]), calendar_id=cal_id,
                        kind=_kind_for(evt_data.get("type")), name=evt_data["name"],
                        start_time=start_dt, end_time=end_dt,
                        location_id=room.id if room else None,
                        organization_id=club_org_id,
                        presenters_raw=presenters_raw,
                        source_id=source_id,
                    )
                    db_session.add(existing_evt)
                else:
                    existing_evt.name = evt_data["name"]
                    existing_evt.kind = _kind_for(evt_data.get("type"))
                    existing_evt.start_time = start_dt; existing_evt.end_time = end_dt
                    existing_evt.location_id = room.id if room else existing_evt.location_id
                    existing_evt.presenters_raw = presenters_raw
                    existing_evt.source_id = source_id
                    if club_org_id and not existing_evt.organization_id:
                        existing_evt.organization_id = club_org_id

                await db_session.flush()

                # --- Sync Class Group Links (secondary EventOrganization rows) ---
                await db_session.execute(delete(EventOrganization).where(EventOrganization.event_id == existing_evt.id))
                if matched_class_group_ids:
                    db_session.add_all([
                        EventOrganization(event_id=existing_evt.id, organization_id=cg_id)
                        for cg_id in matched_class_group_ids
                    ])

                # --- Sync resolved professor links ---
                await db_session.execute(delete(EventPresenter).where(EventPresenter.event_id == existing_evt.id))
                if matched_professor_ids:
                    db_session.add_all([
                        EventPresenter(event_id=existing_evt.id, person_id=pid)
                        for pid in matched_professor_ids
                    ])

                await db_session.flush()
                total_events_loaded += 1

            if progress and task_id: progress.update(task_id, advance=1)

        await db_session.flush()
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=total_events_loaded)
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Agenda: Done ({total_events_loaded} events).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_agenda(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
