import asyncio
import json
import os
import httpx
from casint import AsyncCASClient
from trombint import AsyncTrombiClient
from trombint.config import ETUDIANTS_URL

# Paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
SCRAPS_DIR = os.path.join(BASE_DIR, "data/scraps")
PROFILES_DIR = os.path.join(BASE_DIR, "data/private_assets/profiles")
LOGOS_DIR = os.path.join(BASE_DIR, "data/assets/logos")

os.makedirs(PROFILES_DIR, exist_ok=True)
os.makedirs(LOGOS_DIR, exist_ok=True)

IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp", ".svg", ".gif"]

def _has_image(name: str, directory: str) -> bool:
    for ext in IMAGE_EXTENSIONS:
        if os.path.exists(os.path.join(directory, f"{name}{ext}")):
            return True
    return False

async def download_image(client: httpx.AsyncClient, url: str, name: str, directory: str, referer: str = "") -> bool:
    try:
        headers = {'Referer': referer} if referer else {}
        response = await client.get(url, headers=headers)
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            ext = ".jpg"
            if "image/png" in content_type: ext = ".png"
            elif "image/svg+xml" in content_type: ext = ".svg"
            elif "image/webp" in content_type: ext = ".webp"
            elif "image/gif" in content_type: ext = ".gif"
            
            dest = os.path.join(directory, f"{name}{ext}")
            with open(dest, 'wb') as f:
                f.write(response.content)
            return True
    except: pass
    return False

async def scrape_media(cas_client: AsyncCASClient, progress=None, task_id=None, config: dict = None, log=print):
    concurrency = config.get("concurrency", 5) if config else 5
    delay = config.get("delay", 0.1) if config else 0.1

    # 1. Collect targets from JSON scraps (Decoupled from DB)
    targets = [] # List of (url, filename, directory, referer)
    
    # Students
    students_path = os.path.join(SCRAPS_DIR, "students.json")
    if os.path.exists(students_path):
        with open(students_path, "r", encoding="utf-8") as f:
            for s in json.load(f):
                uid = s.get("uid")
                url = s.get("photo_url") or s.get("profile_picture_path")
                if uid and url and not _has_image(uid, PROFILES_DIR):
                    targets.append((url, uid, PROFILES_DIR, ETUDIANTS_URL))

    # Clubs
    clubs_path = os.path.join(SCRAPS_DIR, "clubs.json")
    if os.path.exists(clubs_path):
        with open(clubs_path, "r", encoding="utf-8") as f:
            for c in json.load(f):
                name = c.get("name")
                url = c.get("logo_url")
                if name and url and not _has_image(name, LOGOS_DIR):
                    targets.append((url, name, LOGOS_DIR, ""))

    if not targets:
        if progress and task_id:
            progress.update(task_id, description="  [green]Harvest Media: All assets present.[/green]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Harvest Media: Downloading {len(targets)} assets...[/blue]", total=len(targets), completed=0)

    log(f"Harvesting [cyan]{len(targets)}[/cyan] missing portraits and logos...")

    sem = asyncio.Semaphore(concurrency)
    
    u = config.get("username") or os.getenv("CAS_USERNAME") if config else os.getenv("CAS_USERNAME")
    p = config.get("password") or os.getenv("CAS_PASSWORD") if config else os.getenv("CAS_PASSWORD")
    t_client = AsyncTrombiClient(username=u, password=p)
    
    async with t_client:
        cas = await t_client._cas_client()
        client = cas._client
        
        async def work(url, name, directory, referer):
            async with sem:
                await download_image(client, url, name, directory, referer)
                if progress and task_id: progress.update(task_id, advance=1)
                if delay > 0: await asyncio.sleep(delay)

        await asyncio.gather(*(work(*t) for t in targets))

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Harvest Media: Complete.[/green]")
