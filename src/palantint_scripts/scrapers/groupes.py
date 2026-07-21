"""
SI-Etudiants Playwright Group Scraper
====================================
Scrapes student group memberships directly from the SI-Etudiants Annuaire (Directory).
This is much more accurate and comprehensive than the SRNI dashboard, and it leverages
active relationship filters to exclude historical alumni.
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime
from bs4 import BeautifulSoup
from casint import AsyncCASClient
from playwright.async_api import async_playwright
from agendint import AgendaClient

# ── Configuration ────────────────────────────────────────────────────────────
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(DATA_DIR, "groupes.json")

logger = logging.getLogger("palantint-scrapers-groupes")

# ── Playwright Scraper Helper ──────────────────────────────────────────────────

async def wait_for_ajax_spinner(frame, timeout_ms=8000):
    try:
        await frame.wait_for_selector("#divClock", state="visible", timeout=800)
        await frame.wait_for_selector("#divClock", state="hidden", timeout=timeout_ms)
    except Exception:
        await asyncio.sleep(1.5)

async def scrape_group_roster(page, group_id, group_name, log):
    """
    Scrapes the active student relations for a specific group from the SI-Etudiants Annuaire.
    Navigates to Dossier.aspx and waits for the nested iframe to load.
    """
    container_url = f"https://si-etudiants.imtbs-tsp.eu/OpDotNet/eplug/Annuaire/Navigation/Dossier/Dossier.aspx?IdObjet={group_id}&IdTypeObjet=28"
    
    try:
        await page.goto(container_url, timeout=45000, referer="https://si-etudiants.imtbs-tsp.eu/OpDotNet/Noyau/Default.aspx?")
        
        # Wait for the frame to load
        await page.wait_for_selector("iframe[name='frm0'], iframe[src*='OngletStandard']", timeout=15000)
        
        frm0 = None
        for frame in page.frames:
            if "frm0" in frame.name or "OngletStandard" in frame.url:
                frm0 = frame
                break
        if not frm0:
            log(f"    [yellow]Warning: Frame 'frm0' not found for group: {group_name}[/yellow]")
            return None
            
        target_context = frm0
    except Exception as e:
        log(f"    [red]Error navigating to group {group_name}: {e} (Current Page URL: {page.url})[/red]")
        return None
            
    # 3. Apply 'En cours' (active relations) filter via AJAX postback
    try:
        ajax_triggered = await target_context.evaluate("""() => {
            const select = document.getElementById('ctl04_ddlAffichage') || document.querySelector("select[id*='ddlAffichage']");
            if (select && select.value !== 'ec') {
                select.value = 'ec';
                FctUcPaveRelations.OnPageChanged(true);
                WebForm_DoCallback(
                    'ctl04',
                    'AffichageChange,' + getObj('ctl04_NbLignes').value + ',' + FctUcPaveRelations.GetCheckedItems() + '**ec',
                    FctUcPaveRelations.OnCallBackReturn,
                    null,
                    FctUcPaveRelations.CallBackError,
                    false
                );
                return true;
            }
            return false;
        }""")
        if ajax_triggered:
            await wait_for_ajax_spinner(target_context)
            await page.wait_for_timeout(500)
    except Exception:
        pass

    # 4. Adjust NbLignes limit to 1000 to fetch all active student relations on one page
    try:
        limit_triggered = await target_context.evaluate("""() => {
            const input = document.getElementById('ctl04_NbLignes') || document.querySelector("input[id*='NbLignes']");
            if (input && input.value !== '1000') {
                input.value = '1000';
                FctUcPaveRelations.NbLignesChanged(true);
                return true;
            }
            return false;
        }""")
        if limit_triggered:
            await wait_for_ajax_spinner(target_context)
            await page.wait_for_timeout(500)
    except Exception:
        pass

    # 5. Parse grid contents
    content = await target_context.content()
    soup = BeautifulSoup(content, "html.parser")
    
    members = []
    seen_ids = set()
    links = soup.find_all("a")
    for link in links:
        onclick = link.get("onclick") or ""
        # Match ouvrirDossierObjet or affDossier (target_id, type_id)
        match = re.search(r"(?:ouvrirDossierObjet|affDossier)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", onclick)
        if match:
            student_id = match.group(1)
            target_type = int(match.group(2))
            student_name = link.get_text(strip=True)
            if target_type == 26 and student_name and student_id not in seen_ids:
                seen_ids.add(student_id)
                members.append({
                    "name": student_name,
                    "id_objet": student_id,
                    "relation": "Membre"
                })
    return members

# ── Scraper Orchestrator ───────────────────────────────────────────────────────

async def scrape_groupes(cas_client: AsyncCASClient, progress=None, task_id=None, config: dict = {}, log=print):
    """
    Scrapes SI-Etudiants group memberships for EI1/EI2 cohorts using Playwright and AsyncCASClient cookies.
    Implements hydration logic to skip already scraped groups unless full_sync is requested.
    """
    delay = config.get("delay", 0.2)
    full_sync = config.get("full_sync", False)
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Syncing Groups: Initializing SI session...[/blue]")

    # 1. Load existing data for hydration
    scraped_groups = []
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
                scraped_groups = json.load(f)
            log(f"  [cyan]ℹ Hydrated {len(scraped_groups)} groups from local storage.[/cyan]")
        except Exception as e:
            log(f"  [yellow]Warning: Failed to load existing groups for hydration: {e}[/yellow]")
    
    existing_map = {str(g["id"]): g for g in scraped_groups}

    # 2. Authenticate AgendaClient
    si_client = AgendaClient(cookies=cas_client.cookies)
    u = config.get("username") or os.getenv("CAS_USERNAME")
    p = config.get("password") or os.getenv("CAS_PASSWORD")
    
    if not u or not p:
        log("  [red]Critical: No credentials provided for SI Ecoles login.[/red]")
        raise RuntimeError("Missing Credentials")

    if not await si_client.login(username=u, password=p):
        log("  [red]Critical: SI Ecoles rejected the session. Check credentials.[/red]")
        raise RuntimeError("SI Ecoles Authentication Failed")

    log(f"  [green]SI Session Established (Group: {si_client.id_groupe}).[/green]")

    # 3. Discover all groups from pre-saved topology manifest
    groups_all_path = os.path.join(os.path.dirname(__file__), "groups_all.json")
    if not os.path.exists(groups_all_path):
        log("  [red]Critical: groups_all.json manifest not found in scraper directory.[/red]")
        raise RuntimeError("Missing groups_all.json manifest")
        
    try:
        with open(groups_all_path, "r", encoding="utf-8") as f:
            all_groups = json.load(f)
    except Exception as e:
        log(f"  [red]Failed to load local manifest: {e}[/red]")
        raise RuntimeError(f"Failed to load groups manifest: {e}")

    # 4. Filter groups for target cohorts
    all_targets = []
    for g in all_groups:
        id_str = str(g.get("id", ""))
        name = g.get("name", "")
        name_lower = name.lower()
        
        is_main_cohort = name in ["CL_FI-EI1", "CL_FI-EI2"]
        is_project_subgroup = id_str.isdigit() and int(id_str) >= 100000 and any(x in name_lower for x in ["ei1", "ei2"])
        is_gp_prefix = name_lower.startswith("gp-")
        has_cohort_marker = any(x in name_lower for x in ["ei1", "ei2"])
        has_subgroup_marker = any(x in name_lower for x in [
            "tdg", "tdgr", "-g1", "-g2", "-g3", "-g4",
            "-tpa", "-tpb", "-tpc", "-tpd", "-tpe", "-tpf",
            "-tpg", "-tph", "-tpi", "-tpj",
        ])
        is_academic_subgroup = is_gp_prefix and has_cohort_marker and has_subgroup_marker
        is_lsh = any(x in name_lower for x in ["lsh", "hum", "sh-"])
        is_lang = any(x in name_lower for x in ["lang", "lv1", "lv2", "lv3", "ang", "eng", "all", "esp", "chi", "ara", "jap", "fle"])
        is_option = any(x in name_lower for x in ["option", "voie", "parcours", "dd", "double-diplome"])
        is_cohort_level = any(x in name_lower for x in ["1a", "2a", "ei1", "ei2", "em1", "em2", "p1", "p2", "p3", "p4", "fisa", "app"])
        
        is_target_extra = is_gp_prefix and (is_lsh or is_lang or is_option) and is_cohort_level
        
        if is_main_cohort or is_project_subgroup or is_academic_subgroup or is_target_extra:
            all_targets.append({"id": id_str, "name": name})

    # 5. Determine which groups actually need scraping
    target_groups = []
    if full_sync:
        target_groups = all_targets
        log(f"  [yellow]⚠ Full sync requested. Scoping {len(target_groups)} groups for re-download.[/yellow]")
    else:
        for t in all_targets:
            if t["id"] not in existing_map:
                target_groups.append(t)
        
        skipped = len(all_targets) - len(target_groups)
        if skipped > 0:
            log(f"  [green]✓ Skipping {skipped} already hydrated groups.[/green]")
        log(f"  [blue]Scoping {len(target_groups)} new/missing groups for scraping.[/blue]")

    if not target_groups:
        log("  [bold green]✓ All target groups are already hydrated. Nothing to do.[/bold green]")
        if progress and task_id:
            progress.update(task_id, description="  [green]Syncing Groups: Already up to date.[/green]", completed=1, total=1)
        return

    # 6. Scrape group rosters using Playwright
    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Syncing Groups: Scraping rosters...[/blue]", total=len(target_groups), completed=0)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        
        # Authenticate Playwright context
        pw_cookies = []
        for cookie in si_client.cookies.jar:
            pw_cookies.append({
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or "si-etudiants.imtbs-tsp.eu",
                "path": cookie.path or "/",
                "secure": cookie.secure or False,
                "httpOnly": "HttpOnly" in str(cookie)
            })
        await context.add_cookies(pw_cookies)
        
        scrape_page = await context.new_page()
        await scrape_page.goto("https://si-etudiants.imtbs-tsp.eu/OpDotNet/Noyau/Default.aspx?")
        await scrape_page.wait_for_timeout(1000)

        try:
            for idx, group_item in enumerate(target_groups):
                gid, gname = group_item["id"], group_item["name"]
                
                if progress and task_id:
                    progress.update(task_id, description=f"  [blue]Syncing Groups: Scraping {gname}...[/blue]")
                    
                members = await scrape_group_roster(scrape_page, gid, gname, log)
                
                if members is not None:
                    # Update map and save incrementally only on success
                    existing_map[gid] = {
                        "id": gid, 
                        "name": gname, 
                        "members": members,
                        "last_sync": datetime.now().isoformat()
                    }
                    
                    # Periodically save to prevent data loss on crash
                    if (idx + 1) % 5 == 0 or (idx + 1) == len(target_groups):
                        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                            json.dump(list(existing_map.values()), f, indent=4, ensure_ascii=False)
                    
                    log(f"    [magenta]Scraped {gname}:[/magenta] [cyan]{len(members)} members.[/cyan]")
                else:
                    log(f"    [red]Skipped {gname} due to retrieval error.[/red]")
                
                if progress and task_id: progress.update(task_id, advance=1)
                if delay > 0: await asyncio.sleep(delay)

        except Exception as err:
            log(f"  [red]Scraper Error: {err}[/red]")
        finally:
            await browser.close()

    log(f"  [bold green]✓ Sync complete. Total hydrated groups: {len(existing_map)}.[/bold green]")
    
    if progress and task_id:
        progress.update(task_id, description=f"  [green]Syncing Groups: Success.[/green]")

# ── Standalone Execute ───────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    
    async def main():
        u = os.getenv("CAS_USERNAME")
        p = os.getenv("CAS_PASSWORD")
        if not u or not p:
            import getpass
            u = input("User: ")
            p = getpass.getpass("Pass: ")
        cas = AsyncCASClient("cas6")
        await cas.login(u, p)
        await scrape_groupes(cas, config={"delay": 0.1})
        
    asyncio.run(main())
