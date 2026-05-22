import asyncio
import json
import os
import re
from datetime import datetime, date, timedelta
from agendint.api import get_calendars, get_events
from agendint.client import SIClient
from casint import CASClient

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps/agenda"))
os.makedirs(DATA_DIR, exist_ok=True)
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

def _load_index():
    if os.path.exists(INDEX_PATH):
        try:
            with open(INDEX_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {}

def _save_index(data):
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def _save_calendar(cal_id, events):
    output_path = os.path.join(DATA_DIR, f"{cal_id}.json")
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
            # "students": get_val(evt, 'students', []), # No longer stored per user request
            "groups": get_val(evt, 'groups', []),
            "status": get_val(evt, 'status', ''),
            "details_loaded": get_val(evt, 'details_loaded', False)
        })
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(events_data, f, indent=4, ensure_ascii=False)

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

async def scrape_agenda(cas_client: CASClient, progress=None, task_id=None, config: dict = None, log=print):
    index_data = _load_index()
    try:
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Initializing session...[/blue]")
        
        si_client = SIClient(cookies=cas_client.cookies)
        # Modern Login using Credentials from Config (set by TUI)
        u = config.get("username") or os.getenv("CAS_USERNAME")
        p = config.get("password") or os.getenv("CAS_PASSWORD")
        
        if not u or not p:
            log("  [red]Critical: No credentials provided for SI Ecoles login.[/red]")
            raise RuntimeError("Missing Credentials")

        if not await si_client.login(username=u, password=p, delay=1.0):
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
            start_date = date.today()
            end_date = start_date + timedelta(weeks=2)
        else:
            # Broader default: -1 month to +3 months
            start_date = date.today() - timedelta(weeks=4) 
            end_date = start_date + timedelta(weeks=16)

        # 1. Discover ALL Calendars
        if agenda_mode == "specific" and custom_id:
            from agendint.models import Calendar
            target_cals = [Calendar(id=custom_id, name=f"Custom: {custom_id}", category="Custom")]
        else:
            if progress and task_id:
                progress.update(task_id, description="  [blue]Scraping Agenda: Scanning all available agendas...[/blue]")
            try:
                all_calendars = await get_calendars(si_client)
                if agenda_mode == "all":
                    # Capture EVERYTHING (Users, Projects, Resources) to find the 40+ agendas
                    target_cals = all_calendars
                else:
                    target_cals = [c for c in all_calendars if c.category == 'Projets']
                
                log(f"  [green]✓ Discovered {len(target_cals)} agendas across all categories.[/green]")
            except Exception as e:
                log(f"  [red]Discovery Failed: {e}. Falling back to default projet.[/red]")
                from agendint.models import Calendar
                target_cals = [Calendar(id="PRJ67059", name="Planning IMT-BS/TSP", category="Projets")]
        
        if not target_cals:
            log("  [red]Critical: No agendas found to scrape.[/red]")
            if progress and task_id:
                progress.update(task_id, description="  [red]Scraping Agenda: 0 agendas found.[/red]", completed=1, total=1)
            return

        # PHASE 1: Exhaustive Event Harvesting
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Harvesting events...[/blue]", total=len(target_cals), completed=0)
        
        for cal in target_cals:
            try:
                # Load existing events if they exist to avoid overwriting hydrated details
                existing_events = _load_calendar(cal.id)
                events_by_id = {e["id"]: e for e in existing_events if e.get("id")}
                
                # Fetch new raw list from SI
                new_raw_events = await get_events(si_client, cal.id, start_date, end_date, config=config)
                
                if not new_raw_events:
                    # Don't log warning for every user, only if total count is 0
                    pass
                else:
                    log(f"  [magenta]Syncing {cal.name}:[/magenta] [cyan]{len(new_raw_events)} events found.[/cyan]")

                merged_list = []
                to_hydrate = []
                for ne in new_raw_events:
                    eid = getattr(ne, "id", "")
                    ne_dict = ne.model_dump() if hasattr(ne, "model_dump") else ne.__dict__.copy()
                    
                    if eid in events_by_id and events_by_id[eid].get("details_loaded") and not full_sync:
                        ne_dict.update({
                            "status": events_by_id[eid].get("status") or "",
                            "trainers": events_by_id[eid].get("professors") or events_by_id[eid].get("trainers") or [],
                            "groups": events_by_id[eid].get("groups") or [],
                            "room": events_by_id[eid].get("room") or ne_dict.get("room"),
                            "details_loaded": True
                        })
                    
                    merged_list.append(ne_dict)
                    if not ne_dict.get("details_loaded"):
                        to_hydrate.append(ne_dict)

                if to_hydrate:
                    log(f"    [cyan]-> Hydrating {len(to_hydrate)} new events...[/cyan]")
                    from agendint.api import get_event_details
                    from agendint.models import Event as AgEvent
                    
                    sem = asyncio.Semaphore(concurrency)
                    async def hydrate_one(item_dict):
                        async with sem:
                            try:
                                ag_evt = AgEvent(
                                    id=item_dict.get("id"),
                                    calendar_id=item_dict.get("calendar_id") or cal.id,
                                    name=item_dict.get("name"),
                                    type=item_dict.get("type"),
                                    date=item_dict.get("date"),
                                    start_time=item_dict.get("start_time"),
                                    end_time=item_dict.get("end_time"),
                                    raw_time=item_dict.get("raw_time") or "",
                                    room=item_dict.get("room"),
                                    details_loaded=False
                                )
                                updated = await get_event_details(si_client, ag_evt, cal.id)
                                item_dict.update({
                                    "status": updated.status or "",
                                    "trainers": updated.trainers or [],
                                    "groups": updated.groups or [],
                                    "room": updated.room or item_dict.get("room"),
                                    "details_loaded": True
                                })
                            except Exception as ex:
                                log(f"      [red]Failed to hydrate event {item_dict.get('id')}: {ex}[/red]")
                            if delay > 0:
                                await asyncio.sleep(delay)
                                
                    await asyncio.gather(*[hydrate_one(item) for item in to_hydrate])

                _save_calendar(cal.id, merged_list)
                
                # Update index state
                is_hydrated = all(e.get("details_loaded") for e in merged_list) if merged_list else True
                index_data[cal.id] = {
                    "name": cal.name,
                    "event_count": len(merged_list),
                    "status": "hydrated" if is_hydrated else "raw",
                    "last_sync": datetime.now().isoformat()
                }
                _save_index(index_data)
                
            except Exception as e:
                log(f"  [red]Failed to harvest {cal.name}: {e}[/red]")
            
            if progress and task_id: progress.update(task_id, advance=1)
            if delay > 0: await asyncio.sleep(delay)


    except asyncio.CancelledError:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Scraping Agenda: Paused. Progress saved.[/yellow]")
        raise
    except Exception as e:
        log(f"[red]Agenda Scraper Error: {e}[/red]")
        raise e
    finally:
        _save_index(index_data)

async def main():
    from dotenv import load_dotenv
    load_dotenv()
    u = os.getenv("CAS_USERNAME")
    p = os.getenv("CAS_PASSWORD")
    if not u or not p:
        import getpass
        u, p = input("User: "), getpass.getpass("Pass: ")
    cas = CASClient(service_url="https://si-etudiants.imtbs-tsp.eu/OpDotNet/Noyau/Login.aspx?auth=SAMLv2ProviderConfiguration")
    if await cas.login(username=u, password=p):
        # We don't even need to pass cas to scrape_agenda if we use it here or pass its cookies
        await scrape_agenda(cas, config={"delay": 0.05, "agenda_mode": "all", "concurrency": 20, "hydrate": True})

if __name__ == "__main__":
    asyncio.run(main())
