import asyncio
import json
import os
import re
from datetime import datetime, date, timedelta
from agendint.api import get_calendars, get_events, get_event_details
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
            "students": get_val(evt, 'students', []),
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

async def scrape_agenda(cas_client: CASClient, progress=None, task_id=None, delay: float = 0.2, context=None, force_reverify: bool = False):
    index_data = _load_index()
    try:
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Initializing session...[/blue]")
        
        si_client = SIClient(cookies=cas_client.cookies)
        await si_client.login()

        agenda_mode = getattr(context, "agenda_mode", "all") if context else "all"
        custom_id = getattr(context, "agenda_custom_id", None) if context else None
        concurrency = getattr(context, "concurrency", 5) if context else 5
        log = getattr(context, "log", print) if context else print

        if agenda_mode == "quick":
            start_date = date.today()
            end_date = start_date + timedelta(weeks=2)
        else:
            start_date = date.today() - timedelta(weeks=2) 
            end_date = start_date + timedelta(weeks=12)

        # 1. Scan Calendars
        if agenda_mode == "specific" and custom_id:
            from agendint.models import Calendar
            target_cals = [Calendar(id=custom_id, name=f"Custom: {custom_id}", category="Custom")]
        else:
            if progress and task_id:
                progress.update(task_id, description="  [blue]Scraping Agenda: Scanning available calendars...[/blue]")
            try:
                all_calendars = await get_calendars(si_client)
                target_cals = [c for c in all_calendars if c.category == 'Projets']
            except Exception:
                from agendint.models import Calendar
                target_cals = [Calendar(id="PRJ67059", name="Planning IMT-BS/TSP", category="Projets")]
        
        if not target_cals:
            if progress and task_id:
                progress.update(task_id, description="  [yellow]Scraping Agenda: No calendars found.[/yellow]", completed=1, total=1)
            return

        # PHASE 1: Sync Event Lists
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Syncing {len(target_cals)} calendars...[/blue]", total=len(target_cals), completed=0)
        
        for cal in target_cals:
            try:
                new_events = await get_events(si_client, cal.id, start_date, end_date)
                existing_events = _load_calendar(cal.id)
                events_by_id = {e["id"]: e for e in existing_events}
                
                merged_list = []
                for ne in new_events:
                    eid = getattr(ne, "id", "")
                    if eid in events_by_id:
                        ne_dict = ne.model_dump() if hasattr(ne, "model_dump") else ne.__dict__
                        ne_dict["details_loaded"] = events_by_id[eid].get("details_loaded", False)
                        merged_list.append(ne_dict)
                    else: merged_list.append(ne)

                _save_calendar(cal.id, merged_list)
                index_data[cal.id] = {
                    "name": cal.name,
                    "event_count": len(merged_list),
                    "status": "raw" if any(not getattr(e, "details_loaded", False) for e in merged_list) else "hydrated"
                }
                _save_index(index_data)
            except Exception as e:
                log(f"[red]Failed to fetch {cal.name}: {e}[/red]")
            
            if progress and task_id: progress.update(task_id, advance=1)
            if delay > 0: await asyncio.sleep(delay)

        # PHASE 2: Hydrate Details
        to_hydrate_ids = [k for k, v in index_data.items() if v.get("status") != "hydrated" or force_reverify]
        total_to_process = 0
        cals_to_process = []
        for cid in to_hydrate_ids:
            events = [MockEvent(e) for e in _load_calendar(cid)]
            pending = [e for e in events if not getattr(e, "details_loaded", False) or force_reverify]
            if pending:
                total_to_process += len(pending)
                cals_to_process.append((cid, index_data[cid]["name"], pending, events))

        if not cals_to_process:
            if progress and task_id: progress.update(task_id, description="  [green]Scraping Agenda: Done.[/green]", completed=1, total=1)
            return

        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Agenda: Hydrating {total_to_process} events...[/blue]", total=total_to_process, completed=0)

        CHUNK_SIZE = concurrency * 2
        for cal_id, cal_name, pending_events, all_events in cals_to_process:
            num_pending = len(pending_events)
            for i in range(0, num_pending, CHUNK_SIZE):
                chunk = pending_events[i : i + CHUNK_SIZE]
                if progress and task_id:
                    progress.update(task_id, description=f"  [blue]Scraping Agenda: Hydrating {cal_name} ({i+len(chunk)}/{num_pending})...[/blue]")

                sem = asyncio.Semaphore(concurrency)
                async def fetch_detail(evt):
                    async with sem:
                        try:
                            res = await get_event_details(si_client, evt, cal_id)
                            res.details_loaded = True
                            return res
                        except: return evt

                await asyncio.gather(*(fetch_detail(e) for e in chunk))
                _save_calendar(cal_id, all_events)
                index_data[cal_id]["status"] = "hydrating"
                _save_index(index_data)

                if progress and task_id: progress.update(task_id, advance=len(chunk))
                if delay > 0: await asyncio.sleep(max(0.5, delay))
                    
            index_data[cal_id]["status"] = "hydrated"
            _save_index(index_data)

    except asyncio.CancelledError:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Scraping Agenda: Stopping and saving progress...[/yellow]")
        raise
    finally:
        _save_index(index_data)

async def main():
    import getpass
    username = input("Enter CAS Username: ")
    password = getpass.getpass("Enter CAS Password: ")
    cas_client = CASClient()
    await cas_client.login(username=username, password=password)
    await scrape_agenda(cas_client)

if __name__ == "__main__":
    asyncio.run(main())
