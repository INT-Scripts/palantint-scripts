import asyncio
import json
import os
from datetime import datetime
from trombint import AsyncTrombiClient
from casint import AsyncCASClient

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(DATA_DIR, "students.json")

async def scrape_trombint(cas_client: AsyncCASClient, progress=None, task_id=None, config: dict = {}, log=print):
    delay = config.get("delay", 0.1)
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Students: Initializing session...[/blue]")

    # 1. Load existing cache
    cached_students = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                for s in json.load(f):
                    if "uid" in s: cached_students[s["uid"]] = s
        except: pass

    # 2. Sync directory lists
    schools = ["IMT-BS", "TSP"]
    live_list = {}
    school_map = {"IMT-BS": "IMT-BS", "TSP": "Télécom SudParis"}
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Students: Syncing directory lists...[/blue]", total=len(schools), completed=0)

    u = config.get("username") or os.getenv("CAS_USERNAME")
    p = config.get("password") or os.getenv("CAS_PASSWORD")

    async with AsyncTrombiClient(username=u, password=p) as t_client:
        for school_key in schools:
            school_full = school_map.get(school_key, school_key)
            if progress and task_id:
                progress.update(task_id, description=f"  [blue]Scraping Students: Fetching {school_full}...[/blue]")
            
            try:
                fiches = await t_client.students(ecole=school_key)
                for fiche in fiches:
                    if fiche.uid:
                        uid = fiche.uid
                        s = {
                            "uid": uid,
                            "nom_complet": fiche.nom_complet,
                            "email": fiche.email,
                            "photo_url": fiche.photo_url,
                            "details": fiche.infos,
                            "ecole": school_full,
                            "promo": None
                        }
                        
                        # Extract ecole/promo from details list if present
                        details = fiche.infos
                        if len(details) >= 1: 
                            if "année" not in details[0]: s["ecole"] = details[0]
                        if len(details) >= 2: s["promo"] = details[1]
                        elif len(details) >= 1 and "année" in details[0]: s["promo"] = details[0]
                        
                        # Fallback to cache for missing fields, but live data always wins
                        if uid in cached_students:
                            for k, v in cached_students[uid].items():
                                if v and not s.get(k):
                                    s[k] = v
                        
                        live_list[uid] = s
            except Exception as e:
                log(f"[red]TrombINT Error ({school_key}): {e}[/red]")
            
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
    cas = AsyncCASClient("cas6")
    await cas.login(u, p)
    await scrape_trombint(cas, config={"username": u, "password": p})

if __name__ == "__main__":
    asyncio.run(main())
