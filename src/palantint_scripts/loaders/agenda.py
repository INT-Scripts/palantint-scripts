import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from db.database import AsyncSessionLocal
from db.models import Event, EventAttendee, EventOrganization, EventPresenter, ExternalIdentity, Location, Organization, Person
from palantint_scripts.utils import normalize_name
from palantint_scripts.loaders.groupes import _roster_name_parts

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import finish_ingestion_run, get_data_source_id, get_or_create_organization, start_ingestion_run

DATA_DIR = str(SCRAPS_AUTO_DIR / "agenda")

SOURCE_CODE = "agenda_ade"


def _kind_for(raw_type: str) -> str:
    t = (raw_type or "").lower()
    if "exam" in t or "partiel" in t:
        return "EXAM"
    return "COURSE"


async def _get_or_create_room(db_session: AsyncSession, room_name: str, cache: dict[str, Location] | None = None) -> Location:
    room = cache.get(room_name) if cache is not None else None
    if room is None:
        result = await db_session.execute(select(Location).where(Location.kind == "ROOM", Location.code == room_name))
        room = result.scalars().first()
    if not room:
        room = Location(kind="ROOM", code=room_name, name=room_name)
        db_session.add(room)
        await db_session.flush()
    if cache is not None:
        cache[room_name] = room
    return room


async def load_agenda(db_session: AsyncSession, progress=None, task_id=None, log=print):
    index_path = os.path.join(DATA_DIR, "index.json")
    if not os.path.exists(index_path):
        if progress and task_id:
            progress.update(task_id, description=f"  [yellow]Load Agenda: index.json not found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(index_path, "r", encoding="utf-8") as f:
        index_data = json.load(f)
    scope_path = os.path.join(DATA_DIR, "scope.json")
    scope = None
    if os.path.exists(scope_path):
        with open(scope_path, "r", encoding="utf-8") as f:
            scope = json.load(f)
        if scope.get("status") == "running":
            raise RuntimeError("Agenda snapshot is still being scraped")

    # A complete course snapshot is authoritative. A separately requested
    # shared user calendar is only imported when there is no course scope.
    scoped_ids = set(scope["calendar_ids"]) if scope and scope.get("status") == "complete" else None
    cals_to_load = [
        cal_id for cal_id, info in index_data.items()
        if info.get("event_count", 0) > 0
        and (scoped_ids is None or cal_id in scoped_ids)
        and (not cal_id.startswith("USR") or str(info.get("last_sync", "")).startswith(datetime.now().date().isoformat()))
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
    class_group_exact = {cg.name: cg.id for cg in all_class_groups}
    class_group_candidates: dict[str, set[uuid.UUID]] = {}
    for group in all_class_groups:
        class_group_candidates.setdefault(normalize_name(group.name), set()).add(group.id)

    result_prof = await db_session.execute(select(Person).where(Person.kind == "PROFESSOR"))
    all_professors = result_prof.scalars().all()
    professor_map = {normalize_name(f"{p.first_name}{p.last_name}"): p.id for p in all_professors}

    result_people = await db_session.execute(select(Person).where(Person.kind == "STUDENT"))
    learner_candidates: dict[str, set[uuid.UUID]] = {}
    for person in result_people.scalars():
        for name in (f"{person.first_name}{person.last_name}", f"{person.last_name}{person.first_name}"):
            learner_candidates.setdefault(normalize_name(name), set()).add(person.id)
    si_source_id = await get_data_source_id(db_session, "groupes")
    identity_rows = await db_session.execute(
        select(ExternalIdentity).where(ExternalIdentity.source_id == si_source_id)
    )
    si_identity_map = {identity.external_id: identity.person_id for identity in identity_rows.scalars()}
    si_ids_by_person: dict[uuid.UUID, set[str]] = {}
    for external_id, person_id in si_identity_map.items():
        si_ids_by_person.setdefault(person_id, set()).add(external_id)
    result_rooms = await db_session.execute(select(Location).where(Location.kind == "ROOM"))
    room_cache = {room.code: room for room in result_rooms.scalars() if room.code}
    result_events = await db_session.execute(select(Event).where(Event.external_ref.is_not(None)))
    event_cache = {event.external_ref: event for event in result_events.scalars()}

    total_events_loaded = 0
    unresolved_learners = 0
    deactivated_count = 0

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
                if cal_id.startswith("USR") and not (evt_data.get("groups") or evt_data.get("learners")):
                    continue
                event_ref = str(evt_data["id"])
                existing_evt = event_cache.get(event_ref)

                try:
                    t_start = evt_data['start_time'].replace('h', ':')
                    t_end = evt_data['end_time'].replace('h', ':')
                    start_dt = datetime.strptime(f"{evt_data['date']} {t_start}", "%Y-%m-%d %H:%M")
                    end_dt = datetime.strptime(f"{evt_data['date']} {t_end}", "%Y-%m-%d %H:%M")
                    if end_dt < start_dt:
                        end_dt += timedelta(days=1)
                except Exception: continue

                professor_names = evt_data.get("professors", []) or []
                presenters_raw = ", ".join(professor_names) if professor_names else None
                matched_professor_ids = {
                    professor_map[normalize_name(name)]
                    for name in professor_names
                    if normalize_name(name) in professor_map
                }

                room_name = evt_data.get("room")
                if room_name and re.match(r"(?i)^salle\s+indiqu[eé]e?\b", room_name):
                    room_name = None
                room = await _get_or_create_room(db_session, room_name, room_cache) if room_name else None

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

                    matched = False
                    if grp_name in class_group_exact:
                        matched_class_group_ids.add(class_group_exact[grp_name])
                        continue
                    for cand in candidates:
                        group_ids = class_group_candidates.get(cand, set())
                        if len(group_ids) == 1:
                            matched_class_group_ids.update(group_ids)
                            matched = True
                            break
                        elif cand in club_map:
                            club_org_id = club_map[cand]
                            matched = True
                            break
                    if not matched and norm_grp.startswith("gp"):
                        group_org = await get_or_create_organization(
                            db_session, kind="CLASS_GROUP", name=grp_name
                        )
                        class_group_exact[grp_name] = group_org.id
                        class_group_candidates.setdefault(norm_grp, set()).add(group_org.id)
                        matched_class_group_ids.add(group_org.id)

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
                    event_cache[event_ref] = existing_evt
                else:
                    existing_evt.name = evt_data["name"]
                    existing_evt.kind = _kind_for(evt_data.get("type"))
                    existing_evt.is_active = True
                    existing_evt.start_time = start_dt; existing_evt.end_time = end_dt
                    existing_evt.location_id = room.id if room else None
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

                # The SI event detail page carries its actual learner list.
                # It covers course groups whose roster is missing or stale.
                learners = evt_data.get("learners")
                if learners is None and evt_data.get("details_loaded"):
                    learners = []
                if isinstance(learners, list):
                    attendee_ids = set()
                    stable_ids = {
                        normalize_name(name): str(external_id)
                        for name, external_id in (evt_data.get("learner_ids") or {}).items()
                    }
                    for name in learners:
                        normalized = normalize_name(name)
                        if not normalized:
                            continue
                        external_id = stable_ids.get(normalized)
                        person_id = si_identity_map.get(external_id) if external_id else None
                        if person_id is None:
                            candidates = learner_candidates.get(normalized, set())
                            if len(candidates) == 1:
                                candidate = next(iter(candidates))
                                if not external_id or not si_ids_by_person.get(candidate) or external_id in si_ids_by_person[candidate]:
                                    person_id = candidate
                            if person_id is None and external_id:
                                first_name, last_name = _roster_name_parts(name)
                                person = Person(kind="STUDENT", first_name=first_name, last_name=last_name)
                                db_session.add(person)
                                await db_session.flush()
                                person_id = person.id
                            if person_id is not None and external_id:
                                db_session.add(ExternalIdentity(
                                    person_id=person_id, source_id=si_source_id, external_id=external_id
                                ))
                                si_identity_map[external_id] = person_id
                                si_ids_by_person.setdefault(person_id, set()).add(external_id)
                        if person_id is None:
                            unresolved_learners += 1
                            continue
                        learner_candidates.setdefault(normalized, set()).add(person_id)
                        attendee_ids.add(person_id)
                    await db_session.execute(delete(EventAttendee).where(EventAttendee.event_id == existing_evt.id))
                    db_session.add_all([
                        EventAttendee(event_id=existing_evt.id, person_id=person_id)
                        for person_id in attendee_ids
                    ])

                await db_session.flush()
                total_events_loaded += 1

            if progress and task_id: progress.update(task_id, advance=1)

        if os.path.exists(scope_path):
            with open(scope_path, "r", encoding="utf-8") as f:
                scope = json.load(f)
            if scope.get("status") == "complete":
                start_date = scope["start_date"]
                end_date = scope["end_date"]
                for cal_id in scope["calendar_ids"]:
                    info = index_data.get(cal_id, {})
                    if (
                        info.get("start_date") != start_date
                        or info.get("end_date") != end_date
                        or info.get("schema_version") != 2
                    ):
                        raise RuntimeError(f"Incomplete agenda snapshot for calendar {cal_id}")
                present_refs = set()
                scope_day = scope["last_sync"][:10]
                for cal_id in scope["calendar_ids"]:
                    info = index_data[cal_id]
                    if str(info.get("last_sync", "")).startswith(scope_day):
                        event_path = os.path.join(DATA_DIR, f"{cal_id}.json")
                        if os.path.exists(event_path):
                            with open(event_path, "r", encoding="utf-8") as f:
                                present_refs.update(str(event["id"]) for event in json.load(f) if event.get("id"))
                start_dt = datetime.fromisoformat(start_date)
                end_exclusive = datetime.fromisoformat(end_date) + timedelta(days=1)
                old_events = await db_session.execute(
                    select(Event).where(
                        Event.source_id == source_id,
                        Event.start_time >= start_dt,
                        Event.start_time < end_exclusive,
                        Event.is_active.is_(True),
                    )
                )
                for event in old_events.scalars():
                    if str(event.external_ref) not in present_refs:
                        event.is_active = False
                        deactivated_count += 1

        await db_session.flush()
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=total_events_loaded, deactivated=deactivated_count)
        if unresolved_learners:
            log(f"[yellow]{unresolved_learners} learner references lacked a unique identity.[/yellow]")
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
