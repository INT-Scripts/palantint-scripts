import asyncio
import os
import httpx
import json
from typing import List, Optional
from pydantic import BaseModel, ValidationError

from palantint_scripts.config import SCRAPS_AUTO_DIR, ASSETS_DIR

API_BASE_URL = os.getenv("CAL_MINET_URL", "https://cal.minet.net")
ORGS_ENDPOINT = f"{API_BASE_URL}/api/organizations/"
DATA_DIR = str(SCRAPS_AUTO_DIR)
CLUBS_ASSETS_DIR = str(ASSETS_DIR / "clubs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CLUBS_ASSETS_DIR, exist_ok=True)

class OrgLinkSchema(BaseModel):
    name: str
    url: str

class OrgSchema(BaseModel):
    id: str
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None
    logo_url: Optional[str] = None
    type: Optional[str] = None
    parent_id: Optional[str] = None
    color_primary: Optional[str] = None
    color_secondary: Optional[str] = None
    organization_links: List[OrgLinkSchema] = []

async def fetch_organizations(log=print) -> List[OrgSchema]:
    # Retry logic for flaky school network
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(ORGS_ENDPOINT)
                response.raise_for_status()
                raw_data = response.json()
                orgs = []
                for item in raw_data:
                    try:
                        orgs.append(OrgSchema(**item))
                    except ValidationError as e:
                        log(f"[yellow]Validation warning for {item.get('name')}[/yellow]")
                return orgs
        except Exception as e:
            if attempt < 2:
                log(f"[yellow]MiNET API Attempt {attempt+1} failed ({e}). Retrying...[/yellow]")
                await asyncio.sleep(1)
            else: raise e
    return []

async def download_image(client: httpx.AsyncClient, url: str, slug: str, log=print) -> Optional[str]:
    try:
        if not url.startswith("http"):
            url = f"{API_BASE_URL}{url}"
        response = await client.get(url)
        if response.status_code == 200:
            content_type = response.headers.get("Content-Type", "")
            ext = "png"
            if "image/jpeg" in content_type: ext = "jpg"
            elif "image/png" in content_type: ext = "png"
            elif "image/svg+xml" in content_type: ext = "svg"
            elif "image/webp" in content_type: ext = "webp"
            elif "image/gif" in content_type: ext = "gif"
            
            filename = f"{slug}.{ext}"
            filepath = os.path.join(CLUBS_ASSETS_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(response.content)
            return f"/api/assets/clubs/{filename}"
    except Exception as e:
        log(f"[red]Error downloading image {url}: {e}[/red]")
    return None

async def scrape_clubs(progress=None, task_id=None, config: dict = None, log=print):
    """
    Standardized entry point using config dict.
    Scrapes data from cal.minet.net API and saves to JSON.
    """
    delay = config.get("delay", 0.2) if config else 0.2

    if progress and task_id:
        progress.update(task_id, description="  [blue]Scraping Clubs: Connecting...[/blue]")
        
    try:
        orgs = await fetch_organizations(log=log)
        log(f"[green]✓ Fetched {len(orgs)} organizations from MiNET API.[/green]")
    except Exception as e:
        if progress and task_id:
            progress.update(task_id, description=f"  [red]Scraping Clubs: API error: {e}[/red]")
        log(f"[red]Clubs API Error: {e}[/red]")
        raise e

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Scraping Clubs: Downloading assets...[/blue]", total=len(orgs), completed=0)

    org_map = {org.id: org for org in orgs}
    final_data = []

    async with httpx.AsyncClient() as client:
        for org in orgs:
            if progress and task_id: progress.update(task_id, advance=1)
            
            association_of_origin = "Autre"
            if org.parent_id and org.parent_id in org_map:
                association_of_origin = org_map[org.parent_id].name
            elif org.type == "association":
                association_of_origin = "Bureau / Asso Centrale"
                
            local_logo_path = None
            if org.logo_url and org.slug:
                log(f"Scraping metadata & assets for [magenta]{org.name}[/magenta]...")
                local_logo_path = await download_image(client, org.logo_url, org.slug, log)
                if delay > 0:
                    await asyncio.sleep(delay)

            final_data.append({
                "name": org.name,
                "slug": org.slug,
                "description": org.description,
                "logo_url": local_logo_path or org.logo_url,
                "type": org.type,
                "association_of_origin": association_of_origin,
                "color_primary": org.color_primary,
                "color_secondary": org.color_secondary,
                "links": [{"name": l.name, "url": l.url} for l in org.organization_links]
            })

    output_path = os.path.join(DATA_DIR, "clubs.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Scraping Clubs: Done. Saved {len(final_data)} organizations.[/green]")

async def main():
    await scrape_clubs(config={"delay": 0.1})

if __name__ == "__main__":
    asyncio.run(main())
