"""
SRNI Group Scraper
==================
Scrapes student group memberships from the internal SRNI dashboard.
Source: https://srni.telecom-sudparis.eu/indicators/groupes.html

This source is much faster and more reliable than the SI-Etudiants Annuaire.
It provides a single table mapping groups to a list of students with IDs.
"""

import asyncio
import json
import os
import re
import logging
import httpx
from bs4 import BeautifulSoup
from casint import CASClient

# ── Configuration ────────────────────────────────────────────────────────────
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(DATA_DIR, "groupes.json")

logger = logging.getLogger("palantint-scrapers-groupes")

SRNI_BASE = "https://srni.telecom-sudparis.eu/indicators/"
GROUPS_URL = f"{SRNI_BASE}groupes.html"

# ── Scraper Logic ────────────────────────────────────────────────────────────

async def scrape_groupes(cas_client: CASClient, progress=None, task_id=None, config: dict = {}, log=print):
    """
    Scrapes the SRNI groups page and saves to groupes.json.
    """
    delay = config.get("delay", 0.1)
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Syncing Groups: Accessing SRNI...[/blue]")

    # 1. Reuse provided CAS client or authenticate if needed
    # SRNI uses the same main CAS. We prefer the cookies from the provided client.
    cookies = cas_client.cookies if cas_client else None
    
    if not cookies:
        u = config.get("username") or os.getenv("CAS_USERNAME")
        p = config.get("password") or os.getenv("CAS_PASSWORD")
        if not u or not p:
            raise RuntimeError("CAS context is missing and no credentials provided.")
        srni_cas = CASClient(service_url=GROUPS_URL)
        if not await srni_cas.login(username=u, password=p):
            raise RuntimeError("SRNI Authentication Failed. Check your credentials.")
        cookies = srni_cas.cookies

    log(f"  [bold green]SRNI Phase 1:[/bold green] Identity established. Fetching manifest...")
    if delay > 0: await asyncio.sleep(delay)

    # 2. Fetch Manifest
    async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as client:
        response = await client.get(GROUPS_URL)
        if response.status_code != 200:
            raise RuntimeError(f"SRNI Manifest Fetch Failed: HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        
        # 3. Locate results table
        table = soup.find("table", id="table")
        if not table:
            raise RuntimeError("SRNI Parsing Error: Could not locate 'table' element in SRNI response.")

        tbody = table.find("tbody")
        rows = tbody.find_all("tr") if tbody else []
        
        if not rows:
            log("  [yellow]Notice: SRNI manifest is currently empty.[/yellow]")
            # Save empty list to clear stale data
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump([], f)
            return

        groups = []
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Syncing Groups: Indexing {len(rows)} records...[/blue]", total=len(rows), completed=0)

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 2:
                continue
            
            group_name = cols[0].get_text(strip=True)
            students_raw = cols[1].get_text(strip=True)
            
            # Formats: "NOM Prenom (ID), NOM Prenom (ID), ..."
            members = []
            matches = re.finditer(r"([^,]+)\s\((\d+)\)", students_raw)
            for m in matches:
                full_name = m.group(1).strip()
                si_id = m.group(2)
                members.append({
                    "name": full_name,
                    "id_objet": si_id,
                    "relation": "Membre"
                })
            
            groups.append({
                "name": group_name,
                "id": group_name,
                "members": members
            })
            
            if progress and task_id:
                progress.update(task_id, advance=1)

        # 4. Filter & Merge
        # SRNI sometimes has split entries for the same group name.
        merged = {}
        for g in groups:
            name = g["name"]
            if name not in merged:
                merged[name] = g
            else:
                existing_ids = {m["id_objet"] for m in merged[name]["members"]}
                for m in g["members"]:
                    if m["id_objet"] not in existing_ids:
                        merged[name]["members"].append(m)
        
        final_groups = list(merged.values())

        # 5. Save Output
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(final_groups, f, indent=4, ensure_ascii=False)

        log(f"  [bold green]✓ Successfully harvested {len(final_groups)} academic topologies from SRNI.[/bold green]")
        if progress and task_id:
            progress.update(task_id, description=f"  [green]Syncing Groups: Success ({len(final_groups)} found).[/green]")

# ── Standalone Execute ───────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    async def main():
        u = os.getenv("CAS_USERNAME")
        p = os.getenv("CAS_PASSWORD")
        cas = CASClient()
        await scrape_groupes(cas, config={"delay": 0.1})
        
    asyncio.run(main())
