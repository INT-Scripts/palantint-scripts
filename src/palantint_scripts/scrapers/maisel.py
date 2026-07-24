import os
import json
import logging
import asyncio
from maiselint.client import MaiselINT
from casint import CASClient

logger = logging.getLogger("palantint.scrapers.maisel")

from palantint_scripts.config import SCRAPS_AUTO_DIR, PLANS_DIR

SCRAP_DIR = str(SCRAPS_AUTO_DIR)
PLANS_DIR = str(PLANS_DIR)

async def scrape_maisel(cas_client: CASClient = None, progress=None, task_id=None, config: dict = None, log=print):
    delay = config.get("delay", 0.5) if config else 0.5
    
    os.makedirs(SCRAP_DIR, exist_ok=True)
    os.makedirs(PLANS_DIR, exist_ok=True)
    
    logements_path = os.path.join(SCRAP_DIR, "logements.json")
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Apartments: Initializing session...[/blue]")

    u = (config.get("username") if config else None) or os.getenv("CAS_USERNAME")
    p = (config.get("password") if config else None) or os.getenv("CAS_PASSWORD")

    if u and p:
        client = await MaiselINT.create(username=u, password=p)
    elif cas_client:
        # Re-authenticate existing CAS session for Maisel portal service via SSO
        await cas_client.login(service="https://reservations.maisel.imtbs-tsp.eu/carte.php")
        client = MaiselINT(cookies=cas_client.cookies)
    else:
        client = await MaiselINT.create()

    # 1. Scrape Lodging Info
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Apartments: Extracting lodging info...[/blue]", total=100, completed=0)

    def info_progress(current, total, msg):
        if progress and task_id:
            pct = int((current / total) * 50)  # Info scraping is first 50%
            progress.update(task_id, description=f"  [blue]Scraping Apartments: {msg}...[/blue]", completed=pct, total=100)

    try:
        apartments = await client.get_all_apartments(progress_callback=info_progress, delay=delay, config=config)
        with open(logements_path, "w", encoding="utf-8") as f:
            json.dump(apartments, f, ensure_ascii=False, indent=4)
        log(f"Apartment info saved to {logements_path}")
    except Exception as e:
        log(f"[red]Error scraping apartment info: {e}[/red]")

    # 2. Scrape Floor Plans
    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Apartments: Downloading floor plans...[/blue]", completed=50, total=100)

    def plan_progress(current, total, msg):
        if progress and task_id:
            pct = 50 + int((current / total) * 50)  # Plan scraping is second 50%
            progress.update(task_id, description=f"  [blue]Scraping Apartments: {msg}...[/blue]", completed=pct, total=100)

    try:
        await client.download_all_plans(output_dir=PLANS_DIR, progress_callback=plan_progress, delay=delay, config=config)
        log(f"Floor plans downloaded to {PLANS_DIR}")
    except Exception as e:
        log(f"[red]Error downloading floor plans: {e}[/red]")

    if progress and task_id:
        progress.update(task_id, description="  [green]Scraping Apartments: Success.[/green]", completed=100, total=100)
