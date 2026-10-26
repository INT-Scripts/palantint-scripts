import asyncio
import json
import os
import re
from datetime import datetime, date, timedelta
from agendint import AgendaClient, list_calendars, get_events, hydrate_events
from casint import AsyncCASClient

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.checkpoint import ItemCheckpoint

DATA_DIR = str(SCRAPS_AUTO_DIR / "agenda")
os.makedirs(DATA_DIR, exist_ok=True)
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
SCOPE_PATH = os.path.join(DATA_DIR, "scope.json")


def _write_scope(status, target_cals, start_date, end_date, checkpoint):
    if status == "complete":
        today = date.today().isoformat()
        for cal in target_cals:
            info = checkpoint.get(cal.id, {})
            if (
                info.get("schema_version") != 2
                or info.get("start_date") != start_date.isoformat()
                or info.get("end_date") != end_date.isoformat()
                or not str(info.get("last_sync", "")).startswith(today)
                or not os.path.exists(os.path.join(DATA_DIR, f"{cal.id}.json"))
            ):
                raise RuntimeError(f"Calendar {cal.id} is incomplete; refusing to mark the agenda snapshot complete")
    scope = {
        "status": status,
        "calendar_ids": [cal.id for cal in target_cals],
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "last_sync": datetime.now().isoformat(),
    }
    temporary_path = SCOPE_PATH + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as f:
        json.dump(scope, f, ensure_ascii=False)
    os.replace(temporary_path, SCOPE_PATH)

def _save_calendar(cal_id, events):
    output_path = os.path.join(DATA_DIR, f"{cal_id}.json")
    temporary_path = output_path + ".tmp"
    events_data = []
    for evt in events:
        get_val = lambda obj, key, default: getattr(obj, key, default) if not isinstance(obj, dict) else obj.get(key, default)
        events_data.append({
            "id": get_val(evt, 'id', ''),
            "calendar_id": get_val(evt, 'calendar_id', cal_id),
            "name": get_val(evt, 'name', ''),
            "type": get_val(evt, 'type', ''),
            "date": get_val(evt, 'date', ''),
            "start_time": get_val(evt, 'start_time', ''),
            "end_time": get_val(evt, 'end_time', ''),
            "room": get_val(evt, 'room', ''),
            "professors": get_val(evt, 'trainers', get_val(evt, 'professors', [])),
            "learners": get_val(evt, 'learners', []),
            "learner_ids": get_val(evt, 'learner_ids', {}),
            "groups": get_val(evt, 'groups', []),
            "group_ids": get_val(evt, 'group_ids', {}),
            "status": get_val(evt, 'status', ''),
            "details_loaded": get_val(evt, 'details_loaded', False)
        })
    with open(temporary_path, "w", encoding="utf-8") as f:
        json.dump(events_data, f, indent=4, ensure_ascii=False)
    os.replace(temporary_path, output_path)

def _load_calendar(cal_id):
    output_path = os.path.join(DATA_DIR, f"{cal_id}.json")
    if os.path.exists(output_path):
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return []

class MockEvent:
    def __init__(self, data):
        self.__dict__.update(data)
    def model_dump(self):
        return self.__dict__

async def scrape_agenda(cas_client: AsyncCASClient, progress=None, task_id=None, config: dict = None, log=print):
    config = config or {}
    checkpoint = ItemCheckpoint(INDEX_PATH, save_every=1)
    try:
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Initializing session...[/blue]")
        
        u = config.get("username") or os.getenv("CAS_USERNAME")
        p = config.get("password") or os.getenv("CAS_PASSWORD")
        
        if not u or not p:
            log("  [red]Critical: No credentials provided for SI Ecoles login.[/red]")
            raise RuntimeError("Missing Credentials")

        si_client = AgendaClient()
        if not await si_client.login(username=u, password=p):
            log("  [red]Critical: SI Ecoles rejected the session. Check credentials.[/red]")
            if progress and task_id:
                progress.update(task_id, description="  [red]Syncing Agenda: Auth Failed.[/red]", completed=1, total=1)
            raise RuntimeError("SI Ecoles Authentication Failed")

        log(f"  [green]SI Session Established (Group: {si_client.id_groupe}).[/green]")

        agenda_mode = config.get("agenda_mode", "all")
        custom_id = config.get("agenda_custom_id")
        concurrency = config.get("concurrency", 5)
        delay = config.get("delay", 0.2)
        full_sync = config.get("full_sync", False)

        if agenda_mode == "quick":
            # 1 month in the past to 1 month in the future
            start_date = date.today() - timedelta(weeks=4)
            end_date = date.today() + timedelta(weeks=4)
        else:
            # Standard: 1 month in the past to 3 months in the future
            start_date = date.today() - timedelta(weeks=4) 
            end_date = date.today() + timedelta(weeks=12)

        # 1. Discover ALL Calendars
        if agenda_mode == "specific" and custom_id:
            from agendint.models import Calendar
            target_cals = [Calendar(id=custom_id, name=f"Custom: {custom_id}", category="Custom")]
        else:
            if progress and task_id:
                progress.update(task_id, description="  [blue]Scraping Agenda: Scanning all available agendas...[/blue]")
            try:
                all_calendars = await list_calendars(si_client)
                target_cals = [c for c in all_calendars if c.category == 'Projets']
                log(f"  [green]✓ Discovered {len(target_cals)} course calendars.[/green]")
            except Exception as e:
                raise RuntimeError("Agenda calendar discovery failed") from e
        
        if not target_cals:
            log("  [red]Critical: No agendas found to scrape.[/red]")
            if progress and task_id:
                progress.update(task_id, description="  [red]Scraping Agenda: 0 agendas found.[/red]", completed=1, total=1)
            return

        complete_catalog = not (agenda_mode == "specific" and custom_id)
        if complete_catalog:
            _write_scope("running", target_cals, start_date, end_date, checkpoint)

        # Resume only calendars already fetched today for this exact window.
        # A sync on a later day must refresh existing calendars as courses change.
        today = date.today().isoformat()
        if full_sync:
            harvest_cals = target_cals
        else:
            harvest_cals = [
                c for c in target_cals
                if not (
                    checkpoint.done(c.id)
                    and checkpoint.get(c.id, {}).get("schema_version") == 2
                    and checkpoint.get(c.id, {}).get("agenda_mode") == agenda_mode
                    and checkpoint.get(c.id, {}).get("start_date") == start_date.isoformat()
                    and checkpoint.get(c.id, {}).get("end_date") == end_date.isoformat()
                    and str(checkpoint.get(c.id, {}).get("last_sync", "")).startswith(today)
                )
            ]
            skipped = len(target_cals) - len(harvest_cals)
            if skipped > 0:
                log(f"  [green]✓ Skipping {skipped} already hydrated agendas.[/green]")

        if not harvest_cals:
            if complete_catalog:
                _write_scope("complete", target_cals, start_date, end_date, checkpoint)
            log("  [bold green]✓ All target agendas are already hydrated. Nothing to do.[/bold green]")
            if progress and task_id:
                progress.update(task_id, description="  [green]Syncing Agenda: Already up to date.[/green]", completed=1, total=1)
            return

        # PHASE 1: Exhaustive Event Harvesting
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Harvesting events...[/blue]", total=len(harvest_cals), completed=0)

        failed_cals = []
        for cal in harvest_cals:
            try:
                if progress and task_id:
                    progress.update(task_id, description=f"  [blue]Syncing Timetables: [magenta]{cal.name}[/magenta]...[/blue]")
                
                # Fetch new raw list from SI
                new_raw_events = await get_events(si_client, cal.id, start_date, end_date)
                if new_raw_events:
                    # Hydrate events to get details (room, trainers/professors, groups, etc.)
                    await hydrate_events(si_client, new_raw_events, concurrency=concurrency, include_learners=True)
                    for attempt in range(2):
                        missing_events = [evt for evt in new_raw_events if not evt.details_loaded]
                        if not missing_events:
                            break
                        await asyncio.sleep(attempt + 1)
                        await hydrate_events(si_client, missing_events, concurrency=concurrency, include_learners=True)
                    missing = sum(not evt.details_loaded for evt in new_raw_events)
                    if missing:
                        raise RuntimeError(f"{missing} of {len(new_raw_events)} event details could not be loaded")
                
                if new_raw_events:
                    log(f"  [magenta]Syncing {cal.name}:[/magenta] [cyan]{len(new_raw_events)} events found.[/cyan]")
                else:
                    log(f"  [magenta]Syncing {cal.name}:[/magenta] [yellow]0 events found.[/yellow]")

                # Convert to dicts directly
                events_list = [
                    ne.model_dump() if hasattr(ne, "model_dump") else ne.__dict__.copy()
                    for ne in new_raw_events
                ]

                _save_calendar(cal.id, events_list)

                # Record and flush index state so this calendar is skipped on resume
                checkpoint.record(cal.id, {
                    "name": cal.name,
                    "event_count": len(events_list),
                    "status": "hydrated",
                    "schema_version": 2,
                    "agenda_mode": agenda_mode,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "last_sync": datetime.now().isoformat()
                })

            except Exception as e:
                failed_cals.append(cal.id)
                log(f"  [red]Failed to harvest {cal.name}: {e}[/red]")

            if progress and task_id: progress.update(task_id, advance=1)
            if delay > 0: await asyncio.sleep(delay)

        if failed_cals:
            raise RuntimeError(f"Agenda harvest failed for {len(failed_cals)} calendars: {', '.join(failed_cals)}")
        if complete_catalog:
            _write_scope("complete", target_cals, start_date, end_date, checkpoint)


    except asyncio.CancelledError:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Scraping Agenda: Paused. Progress saved.[/yellow]")
        raise
    except Exception as e:
        log(f"[red]Agenda Scraper Error: {e}[/red]")
        raise e
    finally:
        checkpoint.flush()

async def main():
    from dotenv import load_dotenv
    load_dotenv()
    u = os.getenv("CAS_USERNAME")
    p = os.getenv("CAS_PASSWORD")
    if not u or not p:
        import getpass
        u, p = input("User: "), getpass.getpass("Pass: ")
    cas = AsyncCASClient("cas6")
    await cas.login(username=u, password=p)
    # We don't even need to pass cas to scrape_agenda if we use it here or pass its cookies
    await scrape_agenda(cas, config={"delay": 0.05, "agenda_mode": "all", "concurrency": 20, "hydrate": True})

if __name__ == "__main__":
    asyncio.run(main())
