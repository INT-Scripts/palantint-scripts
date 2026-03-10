import asyncio
import json
import os
from datetime import datetime
from trombint.client import TrombINT, ETUDIANTS_URL
from casint import CASClient

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(DATA_DIR, "students.json")

async def scrape_trombint(cas_client: CASClient, progress=None, task_id=None, delay: float = 0.1, context=None):
    log = getattr(context, "log", print) if context else print
    concurrency = getattr(context, "concurrency", 5) if context else 5
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Students: Initializing session...[/blue]")

    t_client = TrombINT(cookies=cas_client.cookies)
    
    # 1. Load existing cache
    cached_students = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                for s in json.load(f):
                    if "uid" in s: cached_students[s["uid"]] = s
        except: pass

    # 2. Sync directory lists
    # Note: TrombINT.parse_students already extracts "details" list containing ecole/promo
    schools = ["IMT-BS", "TSP"]
    live_list = {}
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Students: Syncing directory lists...[/blue]", total=len(schools), completed=0)

    for school in schools:
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Scraping Students: Fetching {school}...[/blue]")
        
        data = {"etu[user]": "", "etu[ecole]": school, "etu[annee]": ""}
        try:
            async with (await t_client.get_client()) as client:
                resp = await client.post(ETUDIANTS_URL, data=data)
                resp.raise_for_status()
                for s in t_client.parse_students(resp.text):
                    if "uid" in s:
                        uid = s["uid"]
                        # Extract ecole/promo from details list if present
                        details = s.get("details", [])
                        if len(details) >= 1: s["ecole"] = details[0]
                        if len(details) >= 2: s["promo"] = details[1]
                        
                        # Merge with cache to preserve any existing info
                        if uid in cached_students:
                            s.update({k: v for k, v in cached_students[uid].items() if v and k != "details"})
                        
                        live_list[uid] = s
        except Exception as e:
            log(f"[red]TrombINT Error ({school}): {e}[/red]")
        
        if progress and task_id: progress.update(task_id, advance=1)
        if delay > 0: await asyncio.sleep(delay)

    # Save unified list
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(list(live_list.values()), f, indent=4, ensure_ascii=False)

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Scraping Students: Success ({len(live_list)} records).[/green]")

async def main():
    import getpass
    u, p = input("User: "), getpass.getpass("Pass: ")
    cas = CASClient(); await cas.login(u, p)
    await scrape_trombint(cas)

if __name__ == "__main__":
    asyncio.run(main())
