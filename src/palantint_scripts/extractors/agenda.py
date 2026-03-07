import asyncio
import json
import os
from datetime import datetime, date, timedelta
from agendint.api import get_events, get_event_details
from agendint.client import SIClient
from casint import CASClient

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../data/scraps/agenda"))
os.makedirs(DATA_DIR, exist_ok=True)

async def extract_agenda(cas_client: CASClient, progress=None, task_id=None, calendar_id: str = "PRJ67059", weeks: int = 4, delay: float = 0.2):
    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Extract Agenda: Initializing session...[/blue]")
    
    si_client = SIClient(cookies=cas_client.cookies)
    start_date = date.today()
    end_date = start_date + timedelta(weeks=weeks)
    
    # 1. Fetch basic events
    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Extract Agenda: Scraping {calendar_id}...[/blue]")
    
    def on_fetch_progress(curr, total, month_date, all_evts):
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Extract Agenda: Scraping {month_date.strftime('%B %Y')}...[/blue]", completed=curr, total=total + 1)

    events = await get_events(si_client, calendar_id, start_date, end_date, progress_callback=on_fetch_progress)
    
    if not events:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Extract Agenda: No events found.[/yellow]", completed=1, total=1)
        return

    # 2. Hydrate details
    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Extract Agenda: Hydrating {len(events)} events...[/blue]", total=len(events), completed=0)
    
    sem = asyncio.Semaphore(1 if delay > 0.5 else 10)
    async def fetch_with_progress(evt):
        async with sem:
            res = await get_event_details(si_client, evt, calendar_id)
            if progress and task_id:
                progress.update(task_id, advance=1)
            if delay > 0:
                await asyncio.sleep(delay)
            return res

    await asyncio.gather(*(fetch_with_progress(e) for e in events))

    # 3. Save to JSON
    output_path = os.path.join(DATA_DIR, f"{calendar_id}.json")
    
    # Convert custom Event objects to dicts
    events_data = []
    for evt in events:
        events_data.append({
            "id": evt.id,
            "calendar_id": evt.calendar_id,
            "name": evt.name,
            "type": evt.type,
            "date": evt.date,
            "start_time": evt.start_time,
            "end_time": evt.end_time,
            "room": getattr(evt, 'room', ''),
            "professors": getattr(evt, 'trainers', []),
            "students": getattr(evt, 'students', [])
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(events_data, f, indent=4, ensure_ascii=False)

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Extract Agenda: Saved {len(events)} events to JSON.[/green]")

async def main():
    import getpass
    username = input("Enter CAS Username: ")
    password = getpass.getpass("Enter CAS Password: ")
    cas_client = CASClient()
    await cas_client.login(username=username, password=password)
    await extract_agenda(cas_client)

if __name__ == "__main__":
    asyncio.run(main())
