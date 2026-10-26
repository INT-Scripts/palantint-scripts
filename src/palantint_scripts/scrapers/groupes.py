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
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from casint import AsyncCASClient
from playwright.async_api import async_playwright
from agendint import AgendaClient

# ── Configuration ────────────────────────────────────────────────────────────
from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.checkpoint import ItemCheckpoint
from palantint_scripts.utils import normalize_name

DATA_DIR = str(SCRAPS_AUTO_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT_PATH = os.path.join(DATA_DIR, "groupes.json")
SCOPE_PATH = SCRAPS_AUTO_DIR / "groupes_scope.json"


def _save_scope(status: str, targets: list[dict]) -> None:
    scope = {
        "status": status,
        "target_ids": [group["id"] for group in targets],
        "last_sync": datetime.now().isoformat(),
    }
    temporary_path = SCOPE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(scope, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary_path, SCOPE_PATH)

logger = logging.getLogger("palantint-scrapers-groupes")


def _agenda_group_targets() -> tuple[dict[str, str], set[str]]:
    """Return current event group object ids and labels from hydrated agendas."""
    agenda_dir = SCRAPS_AUTO_DIR / "agenda"
    index_path = agenda_dir / "index.json"
    if not index_path.exists():
        return {}, set()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    scope_path = agenda_dir / "scope.json"
    scoped_ids = None
    if scope_path.exists():
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        if scope.get("status") == "running":
            raise RuntimeError("Course agenda snapshot is still being scraped")
        if scope.get("status") == "complete":
            scoped_ids = set(scope["calendar_ids"])
    cutoff = datetime.now() - timedelta(days=30)
    ids: dict[str, str] = {}
    labels: set[str] = set()
    for calendar_id, info in index.items():
        if scoped_ids is not None and calendar_id not in scoped_ids:
            continue
        try:
            if datetime.fromisoformat(info["last_sync"]) < cutoff:
                continue
        except (KeyError, ValueError):
            continue
        event_path = agenda_dir / f"{calendar_id}.json"
        if not event_path.exists():
            continue
        for event in json.loads(event_path.read_text(encoding="utf-8")):
            labels.update(normalize_name(name) for name in event.get("groups") or [] if name)
            for name, group_id in (event.get("group_ids") or {}).items():
                if str(group_id).isdigit():
                    ids[str(group_id)] = name
    return ids, labels

# ── SI-Etudiants portal endpoints ────────────────────────────────────────────
# Same portal, same constraints documented in
# int-libraries/packages/annuairint/docs/endpoints.md (§2b/§2c): a fresh
# session rejects direct navigation into the Annuaire until it's been entered
# "for real" once via its main-menu bridge link, and Dossier.aspx additionally
# requires the Referer of an actually-fetched Contenu.aspx page in that same
# session (a generic referer causes a genuine server-side error, not just a
# soft rejection). annuairint implements this correctly for person dossiers
# (IdTypeObjet=25); this scraper needs the same handshake for group dossiers
# (IdTypeObjet=28).
SI_BASE = "https://si-etudiants.imtbs-tsp.eu"
DEFAULT_URL = f"{SI_BASE}/OpDotNet/Noyau/Default.aspx?"
NAVIGATION_URL = f"{SI_BASE}/OpDotNet/Eplug/Annuaire/Navigation/Navigation.aspx"
CONTENU_URL = f"{SI_BASE}/OpDotNet/Eplug/Annuaire/Navigation/Contenu.aspx"
ANNUAIRE_ENTRY_URL = f"{SI_BASE}/OpDotNet/Eplug/Annuaire/Accueil.aspx"
ANNUAIRE_ENTRY_APP_ID = "142"
ANNUAIRE_ENTRY_TYPE_ACCES = "Utilisateur"
ANNUAIRE_ENTRY_ID_LIEN = "306"
DOSSIER_URL = f"{SI_BASE}/OpDotNet/eplug/Annuaire/Navigation/Dossier/Dossier.aspx"
GROUP_DOSSIER_ID_TYPE_OBJET = 28

# The group dossier's "Relations" pave (ucPaveRelations.js) validates NbLignes
# client-side against ctl04_RangeValidator1, whose maximumvalue is observed
# live as "100" — a *different*, smaller cap than the 1000-row ceiling
# documented for the main Annuaire grid (annuairint's NBLIGNES_MAX). Classes
# bigger than this need real pagination via DataGridPager1 (see below), not a
# bigger NbLignes value.
GROUP_RELATIONS_PAGE_SIZE = 100

# ── Playwright Scraper Helper ──────────────────────────────────────────────────

async def wait_for_ajax_spinner(frame, timeout_ms=8000):
    try:
        await frame.wait_for_selector("#divClock", state="visible", timeout=800)
        await frame.wait_for_selector("#divClock", state="hidden", timeout=timeout_ms)
    except Exception:
        await asyncio.sleep(1.5)

async def warm_up_annuaire(page, id_groupe, log):
    """
    One-time-per-session bootstrap required by the SI-Etudiants portal before
    Contenu.aspx/Dossier.aspx accept direct navigation on a fresh login.
    Returns the Contenu.aspx URL to use as the Referer for subsequent
    Dossier.aspx requests (see module docstring / §2b, §2c).
    """
    entry_url = (
        f"{ANNUAIRE_ENTRY_URL}"
        f"?IdApplication={ANNUAIRE_ENTRY_APP_ID}"
        f"&TypeAcces={ANNUAIRE_ENTRY_TYPE_ACCES}"
        f"&IdLien={ANNUAIRE_ENTRY_ID_LIEN}"
        f"&groupe={id_groupe}"
    )
    log("  [blue]Syncing Groups: Warming up Annuaire session...[/blue]")
    await page.goto(entry_url, timeout=30000, referer=DEFAULT_URL)
    if "OPErreur.aspx" in page.url:
        raise RuntimeError(f"Annuaire warm-up rejected (landed on {page.url}).")

    await page.goto(CONTENU_URL, timeout=30000, referer=NAVIGATION_URL)
    if "OPErreur.aspx" in page.url or "Login.aspx" in page.url:
        raise RuntimeError(f"Annuaire Contenu.aspx request rejected (landed on {page.url}).")

    return page.url

async def scrape_group_roster(page, group_id, group_name, referer, log):
    """
    Scrapes the active student relations for a specific group from the SI-Etudiants Annuaire.
    Navigates to Dossier.aspx and waits for the nested iframe to load.
    `referer` must be a Contenu.aspx URL actually fetched earlier in this
    session (see warm_up_annuaire) — a generic referer gets the request
    server-side rejected before the iframe ever renders.
    """
    container_url = f"{DOSSIER_URL}?IdObjet={group_id}&IdTypeObjet={GROUP_DOSSIER_ID_TYPE_OBJET}"

    try:
        await page.goto(container_url, timeout=45000, referer=referer)

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

    # 4. Bump the page size to this widget's actual validated max (100, not
    # the 1000 used elsewhere on this portal — see GROUP_RELATIONS_PAGE_SIZE).
    try:
        limit_triggered = await target_context.evaluate(f"""() => {{
            const input = document.getElementById('ctl04_NbLignes') || document.querySelector("input[id*='NbLignes']");
            if (input && input.value !== '{GROUP_RELATIONS_PAGE_SIZE}') {{
                input.value = '{GROUP_RELATIONS_PAGE_SIZE}';
                FctUcPaveRelations.NbLignesChanged(true);
                return true;
            }}
            return false;
        }}""")
        if limit_triggered:
            await wait_for_ajax_spinner(target_context)
            await page.wait_for_timeout(500)
    except Exception:
        pass

    # 5. Parse grid contents, walking DataGridPager1 for classes bigger than
    # one page (each numbered/next/last pager link posts back through
    # WebForm_DoCallback('ctl04$DataGridPager1', '<target>,<current>,<total>', ...)).
    members = []
    seen_ids = set()

    def merge_members(html):
        for link in BeautifulSoup(html, "html.parser").find_all("a"):
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

    content = await target_context.content()
    merge_members(content)

    pager_match = re.search(r"DataGridPager1','\d+,\d+,(\d+)'", content)
    page_count = int(pager_match.group(1)) if pager_match else 1

    current_page = 1
    while current_page < page_count:
        next_page = current_page + 1
        try:
            await target_context.evaluate(f"""() => {{
                OnPageChanged(true, 'Chargement en cours ...');
                WebForm_DoCallback('ctl04$DataGridPager1', '{next_page},{current_page},{page_count}', OnCallBackPageNumberChanged, null, CallBackError, false);
            }}""")
            await wait_for_ajax_spinner(target_context)
            await page.wait_for_timeout(500)
            content = await target_context.content()
            merge_members(content)
            current_page = next_page
        except Exception as e:
            log(f"    [yellow]Warning: pagination stopped early for {group_name} at page {next_page}: {e}[/yellow]")
            break

    return members

# ── Scraper Orchestrator ───────────────────────────────────────────────────────

async def scrape_groupes(cas_client: AsyncCASClient, progress=None, task_id=None, config: dict = {}, log=print):
    """
    Scrapes rosters for groups referenced by current hydrated agenda events.
    """
    delay = config.get("delay", 0.2)
    full_sync = config.get("full_sync", False)
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Syncing Groups: Initializing SI session...[/blue]")

    # 1. Load existing data for hydration
    checkpoint = ItemCheckpoint(
        OUTPUT_PATH,
        save_every=5,
        to_disk=lambda items: list(items.values()),
        from_disk=lambda data: {str(g["id"]): g for g in data},
    )
    log(f"  [cyan]ℹ Hydrated {len(checkpoint.items)} groups from local storage.[/cyan]")

    # 2. Authenticate AgendaClient
    u = config.get("username") or os.getenv("CAS_USERNAME")
    p = config.get("password") or os.getenv("CAS_PASSWORD")
    
    if not u or not p:
        log("  [red]Critical: No credentials provided for SI Ecoles login.[/red]")
        raise RuntimeError("Missing Credentials")

    si_client = AgendaClient()
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

    # 4. Resolve every group used by current course events, across cohorts.
    discovered_ids, active_labels = _agenda_group_targets()
    if not discovered_ids and not active_labels:
        raise RuntimeError("No current agenda group labels found; scrape agendas before groups")
    target_by_id = {group_id: {"id": group_id, "name": name} for group_id, name in discovered_ids.items()}
    labels_with_live_ids = {normalize_name(name) for name in discovered_ids.values()}
    fallback_labels = active_labels - labels_with_live_ids
    for g in all_groups:
        id_str = str(g.get("id", ""))
        name = g.get("name", "")
        if id_str.isdigit() and normalize_name(name) in fallback_labels:
            target_by_id.setdefault(id_str, {"id": id_str, "name": name})
    all_targets = list(target_by_id.values())
    requested_ids = {str(group_id) for group_id in config.get("group_ids", [])}
    if requested_ids:
        all_targets = [group for group in all_targets if group["id"] in requested_ids]
        missing_ids = requested_ids - {group["id"] for group in all_targets}
        if missing_ids:
            raise ValueError(f"Group ids absent from current agendas: {', '.join(sorted(missing_ids))}")
    else:
        # A complete roster snapshot is safe to use for closing stale
        # memberships only after every listed course calendar was refreshed.
        catalog = [c.id for c in await si_client.list_calendars() if c.category == "Projets"]
        agenda_index = json.loads((SCRAPS_AUTO_DIR / "agenda" / "index.json").read_text(encoding="utf-8"))
        today = datetime.now().date().isoformat()
        missing_calendars = [
            cal_id for cal_id in catalog
            if agenda_index.get(cal_id, {}).get("schema_version") != 2
            or not str(agenda_index.get(cal_id, {}).get("last_sync", "")).startswith(today)
            or agenda_index.get(cal_id, {}).get("agenda_mode") in ("quick",)
            or not (SCRAPS_AUTO_DIR / "agenda" / f"{cal_id}.json").exists()
        ]
        if missing_calendars:
            raise RuntimeError(f"Refresh all {len(missing_calendars)} remaining course calendars before full group sync")
        _save_scope("running", all_targets)

    # 5. Determine which groups actually need scraping
    target_groups = []
    if full_sync:
        target_groups = all_targets
        log(f"  [yellow]⚠ Full sync requested. Scoping {len(target_groups)} groups for re-download.[/yellow]")
    else:
        for t in all_targets:
            if not (
                checkpoint.done(t["id"])
                and str(checkpoint.get(t["id"], {}).get("last_sync", "")).startswith(datetime.now().date().isoformat())
            ):
                target_groups.append(t)
        
        skipped = len(all_targets) - len(target_groups)
        if skipped > 0:
            log(f"  [green]✓ Skipping {skipped} already hydrated groups.[/green]")
        log(f"  [blue]Scoping {len(target_groups)} new/missing groups for scraping.[/blue]")

    if not target_groups:
        if not requested_ids:
            _save_scope("complete", all_targets)
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
        await scrape_page.goto(DEFAULT_URL)
        await scrape_page.wait_for_timeout(1000)

        try:
            contenu_referer = await warm_up_annuaire(scrape_page, si_client.id_groupe, log)
        except Exception as e:
            log(f"  [red]Critical: Annuaire warm-up failed: {e}[/red]")
            await browser.close()
            raise RuntimeError(f"Annuaire warm-up failed: {e}")

        failed_groups = []
        try:
            for group_item in target_groups:
                gid, gname = group_item["id"], group_item["name"]

                if progress and task_id:
                    progress.update(task_id, description=f"  [blue]Syncing Groups: Scraping {gname}...[/blue]")

                members = await scrape_group_roster(scrape_page, gid, gname, contenu_referer, log)

                if members is not None:
                    # Record and periodically flush to prevent data loss on crash
                    checkpoint.record(gid, {
                        "id": gid,
                        "name": gname,
                        "members": members,
                        "last_sync": datetime.now().isoformat()
                    })
                    log(f"    [magenta]Scraped {gname}:[/magenta] [cyan]{len(members)} members.[/cyan]")
                else:
                    failed_groups.append(gid)
                    log(f"    [red]Skipped {gname} due to retrieval error.[/red]")

                if progress and task_id: progress.update(task_id, advance=1)
                if delay > 0: await asyncio.sleep(delay)

        except Exception as err:
            log(f"  [red]Scraper Error: {err}[/red]")
            raise
        finally:
            checkpoint.flush()
            await browser.close()

    log(f"  [bold green]✓ Sync complete. Total hydrated groups: {len(checkpoint.items)}.[/bold green]")
    if failed_groups:
        raise RuntimeError(f"Group roster retrieval failed for: {', '.join(failed_groups)}")
    if not requested_ids:
        _save_scope("complete", all_targets)
    
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
