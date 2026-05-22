#!/usr/bin/env python3
"""
PalantINT — Overhauled Data Synchronization Pipeline
===================================================
A robust, state-driven ETL orchestrator for campus data.
"""

import asyncio
import importlib
import os
import sys
import time
import questionary
from dataclasses import dataclass, field
from typing import Optional, Callable, List, Any, Dict

from casint import CASClient
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.table import Table
from rich import box
from rich.live import Live
from questionary import Style
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import AsyncSessionLocal, engine

console = Console()

# ── Styling ──────────────────────────────────────────────────────────────────

custom_style = Style([
    ('qmark', 'fg:#5fafff bold'), ('question', 'bold'), ('answer', 'fg:#5fafff bold'),
    ('pointer', 'fg:#5fafff bold'), ('highlighted', 'fg:#ffffff bg:#005faf bold'), 
    ('selected', 'fg:#5fafff'), ('separator', 'fg:#6c6c6c'), ('instruction', 'fg:#8a8a8a italic'),
])

def apply_nav_keys(q: questionary.Question):
    """Hooks Left/Esc keys into a questionary object for back-navigation."""
    kb = q.application.key_bindings
    if kb:
        @kb.add('left')
        @kb.add('escape')
        def _(event):
            event.app.exit(result="__BACK__")
    return q

# ── Models ───────────────────────────────────────────────────────────────────

@dataclass
class FlowState:
    """Current configuration state of the sync process."""
    phase: str = "scrape_only"
    db_strategy: str = "update"
    selected_ids: List[str] = field(default_factory=list)
    agenda_mode: str = "all"
    agenda_custom_id: Optional[str] = None
    delay: float = 0.2
    concurrency: int = 5
    full_sync: bool = False

    def to_config(self) -> dict:
        """Flattens state into the unified config dict for scrapers."""
        return {
            "delay": self.delay,
            "concurrency": self.concurrency,
            "full_sync": self.full_sync,
            "agenda_mode": self.agenda_mode,
            "agenda_custom_id": self.agenda_custom_id
        }

@dataclass
class PipelineContext:
    cas_client: Optional[CASClient]
    db_session: Optional[AsyncSession]
    progress: Progress
    task_id: int
    config: dict
    log: Callable[[str], None] = lambda x: None

PIPELINE_DOMAINS = [
    {"id": "clubs", "name": "Clubs & Organizations", "scraper": {"name": "Scraping Clubs", "module": "palantint_scripts.scrapers.clubs", "func": "scrape_clubs"}, "loader": {"name": "Update Clubs", "module": "palantint_scripts.loaders.clubs", "func": "load_clubs", "needs_db": True}},
    {"id": "students", "name": "Student Directory", "scraper": {"name": "Scraping Students", "module": "palantint_scripts.scrapers.trombint", "func": "scrape_trombint", "needs_cas": True}, "loader": {"name": "Update Students", "module": "palantint_scripts.loaders.trombint", "func": "load_trombint", "needs_db": True}},
    {"id": "groupes", "name": "Group Topologies", "scraper": {"name": "Scraping Groups", "module": "palantint_scripts.scrapers.groupes", "func": "scrape_groupes", "needs_cas": True}, "loader": {"name": "Update Groups", "module": "palantint_scripts.loaders.groupes", "func": "load_groupes", "needs_db": True}},
    {"id": "agenda", "name": "Timetables", "scraper": {"name": "Scraping Schedules", "module": "palantint_scripts.scrapers.agenda", "func": "scrape_agenda", "needs_cas": True}, "loader": {"name": "Update Schedules", "module": "palantint_scripts.loaders.agenda", "func": "load_agenda", "needs_db": True}},
    {"id": "media", "name": "Media Assets", "scraper": {"name": "Harvesting Media", "module": "palantint_scripts.scrapers.media", "func": "scrape_media", "needs_cas": True}},
    {"id": "apartments", "name": "Apartments", "scraper": {"name": "Scraping Apartments", "module": "palantint_scripts.scrapers.maisel", "func": "scrape_maisel", "needs_cas": True}, "loader": {"name": "Update Apartments", "module": "palantint_scripts.loaders.apartments", "func": "load_apartments", "needs_db": True}}
]

# ── Step Definitions ─────────────────────────────────────────────────────────

async def step_select_phase(state: FlowState):
    choice = await apply_nav_keys(questionary.select(
        "Select Operation:",
        choices=[
            questionary.Choice("Download External Data (Web -> JSON)", value="scrape_only"),
            questionary.Choice("Update Database (JSON -> DB)", value="load_only"),
            questionary.Choice("Create Backup (DB -> JSON)", value="export_only"),
            questionary.Choice("Cancel", value="EXIT"),
        ],
        style=custom_style
    )).ask_async()
    if not choice or choice == "EXIT": return "EXIT"
    if choice == "__BACK__": return "EXIT" # Start of flow, exit back to main menu
    state.phase = choice
    return "NEXT"

async def step_db_strategy(state: FlowState):
    if state.phase != "load_only": return "SKIP"
    choice = await apply_nav_keys(questionary.select(
        "Database Strategy:",
        choices=[
            questionary.Choice("Standard Update (Synchronize all records)", value="update"),
            questionary.Choice("Clean Reset (Wipe database before updating)", value="purge"),
        ],
        style=custom_style,
        instruction="[Left/Esc: Back]"
    )).ask_async()
    if not choice: return "EXIT"
    if choice == "__BACK__": return "BACK"
    state.db_strategy = choice
    return "NEXT"

async def step_select_domains(state: FlowState):
    if state.phase == "export_only": return "SKIP"
    choice = await apply_nav_keys(questionary.checkbox(
        "Select Data to Process:",
        choices=[
            questionary.Choice(d["name"], value=d["id"], checked=True) for d in PIPELINE_DOMAINS
        ],
        style=custom_style,
        instruction="[Left/Esc: Back]"
    )).ask_async()
    if choice is None: return "EXIT"
    if choice == "__BACK__": return "BACK"
    state.selected_ids = choice
    return "NEXT"

async def step_agenda_config(state: FlowState):
    if state.phase != "scrape_only" or "agenda" not in state.selected_ids: return "SKIP"
    choice = await apply_nav_keys(questionary.select(
        "Schedule Depth:",
        choices=[
            questionary.Choice("Standard (Next 3 months)", value="all"),
            questionary.Choice("Quick (Next 2 weeks only)", value="quick"),
            questionary.Choice("Targeted ID", value="specific"),
        ],
        style=custom_style,
        instruction="[Left/Esc: Back]"
    )).ask_async()
    if not choice: return "EXIT"
    if choice == "__BACK__": return "BACK"
    state.agenda_mode = choice
    if choice == "specific":
        res = await questionary.text("Enter Calendar ID:", style=custom_style).ask_async()
        if res is None: return "EXIT"
        state.agenda_custom_id = res
    return "NEXT"

async def step_velocity(state: FlowState):
    if state.phase != "scrape_only": return "SKIP"
    choice = await apply_nav_keys(questionary.select(
        "Download Velocity:",
        choices=[
            questionary.Choice("Standard (0.2s cooldown, 5 connections)", value=(0.2, 5)),
            questionary.Choice("Safe (1.0s cooldown, 1 connection)", value=(1.0, 1)),
            questionary.Choice("Aggressive (0.0s cooldown, 10 connections)", value=(0.0, 10)),
            questionary.Choice("Custom (Manual parameters)", value="custom"),
        ],
        style=custom_style,
        instruction="[Left/Esc: Back]"
    )).ask_async()
    if not choice: return "EXIT"
    if choice == "__BACK__": return "BACK"
    
    if choice == "custom":
        d_res = await questionary.text("Cooldown (seconds):", default="0.5", style=custom_style).ask_async()
        if d_res is None: return "EXIT"
        state.delay = float(d_res or 0.5)
        
        c_res = await questionary.text("Simultaneous connections:", default="3", style=custom_style).ask_async()
        if c_res is None: return "EXIT"
        state.concurrency = int(c_res or 3)
    else:
        state.delay, state.concurrency = choice
    return "NEXT"

async def step_execution_mode(state: FlowState):
    if state.phase != "scrape_only": return "SKIP"
    choice = await apply_nav_keys(questionary.select(
        "Mode Execution:",
        choices=[
            questionary.Choice("Standard Sync (Only fetch missing/outdated)", value=False),
            questionary.Choice("Deep Sync (Force re-download everything)", value=True),
        ],
        style=custom_style,
        instruction="[Left/Esc: Back]"
    )).ask_async()
    if choice is None: return "EXIT"
    if choice == "__BACK__": return "BACK"
    state.full_sync = choice
    return "NEXT"

# ── Engine ──────────────────────────────────────────────────────────────────

async def run_step_async(step_info, ctx: PipelineContext):
    start = time.time()
    try:
        if step_info.get("module") is None:
            if step_info["name"] == "Wipe Database":
                from db.models import SQLModel
                async with engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.drop_all)
                    await conn.run_sync(SQLModel.metadata.create_all)
            elif step_info["name"] == "Export Database":
                from .exporter import export_db_data
                await export_db_data(ctx.db_session, lambda x: None)
        else:
            mod = importlib.import_module(step_info["module"])
            func = getattr(mod, step_info["func"])
            import inspect
            sig = inspect.signature(func)
            kwargs = {k: v for k, v in {
                "context": ctx, "config": ctx.config, "cas_client": ctx.cas_client, "db_session": ctx.db_session, 
                "session": ctx.db_session, "progress": ctx.progress, "task_id": ctx.task_id, "log": ctx.log
            }.items() if k in sig.parameters}
            await func(**kwargs)

        ctx.progress.update(ctx.task_id, description=f"[green]  {step_info['name']}: Done.[/green]", completed=1, total=1)
        return {"name": step_info["name"], "status": "✅ Done", "reason": f"{time.time()-start:.1f}s"}
    except (asyncio.CancelledError, KeyboardInterrupt):
        ctx.progress.update(ctx.task_id, description=f"[yellow]  {step_info['name']}: Interrupted.[/yellow]", completed=1, total=1)
        return {"name": step_info["name"], "status": "🛑 Aborted", "reason": "Progress Saved"}
    except Exception as e:
        ctx.progress.update(ctx.task_id, description=f"[red]  {step_info['name']}: Failed![/red]", completed=1, total=1)
        return {"name": step_info["name"], "status": "❌ Failed", "reason": str(e)[:60]}

async def run_pipeline():
    db_session, db_context, cas_client = None, None, None
    results = []
    try:
        console.print(Panel.fit("[bold white]PalantINT[/bold white] [blue]Data Synchronization[/blue]\n[dim]Synchronizing registry identities and OSINT vaults.[/dim]", border_style="blue", box=box.HEAVY))
        
        # ── 1. Linear Wizard Flow with History Stack ───────────────────────
        state = FlowState()
        steps = [
            step_select_phase,
            step_db_strategy,
            step_select_domains,
            step_agenda_config,
            step_velocity,
            step_execution_mode
        ]
        
        history = []
        idx = 0
        while idx < len(steps):
            res = await steps[idx](state)
            if res == "EXIT": return
            if res == "NEXT":
                history.append(idx)
                idx += 1
            elif res == "BACK":
                if not history: return # Back out to main menu
                idx = history.pop()
            elif res == "SKIP":
                idx += 1
            if idx < 0: return

        # ── 2. Task Mapping ──────────────────────────────────────────────────
        mode_scrape, mode_load, mode_export = state.phase == "scrape_only", state.phase == "load_only", state.phase == "export_only"
        
        active_scrapers = [d["scraper"] for d in PIPELINE_DOMAINS if d["id"] in state.selected_ids and "scraper" in d] if mode_scrape else []
        active_loaders = []
        if mode_load:
            if state.db_strategy == "purge": active_loaders.append({"name": "Wipe Database", "module": None})
            active_loaders.append({"name": "Setup Infrastructure", "module": "db.seed", "func": "seed_default_data"})
            active_loaders.append({"name": "Anchor Identities", "module": "palantint_scripts.loaders.vault", "func": "anchor_identities"})
            active_loaders.extend([d["loader"] for d in PIPELINE_DOMAINS if d["id"] in state.selected_ids and "loader" in d])
            active_loaders.append({"name": "Restore Research", "module": "palantint_scripts.loaders.vault", "func": "restore_research"})
        
        if mode_export: active_loaders.append({"name": "Export Database", "module": None})

        # ── 3. Authentication Phase ──────────────────────────────────────────
        needs_cas = any(s.get("needs_cas") for s in active_scrapers + active_loaders)
        config = state.to_config() # Initialize config here to pass credentials
        if needs_cas:
            while not cas_client:
                user = await questionary.text("Enter Username:", style=custom_style).ask_async()
                if user is None: return # Allow cancel
                pw = await questionary.password("Enter Password:", style=custom_style).ask_async()
                if pw is None: return # Allow cancel
                console.print("\n[blue]ℹ Authenticating...[/blue]")
                try:
                    client = CASClient(service_url="https://cas6.imtbs-tsp.eu/cas/login")
                    await client.login(username=user, password=pw, delay=state.delay)
                    cas_client = client
                    os.environ["CAS_USERNAME"] = user
                    os.environ["CAS_PASSWORD"] = pw
                    config["username"] = user # Store credentials in config for scrapers
                    config["password"] = pw
                    # Update state too so future to_config() calls preserve them
                    state.credentials = (user, pw)
                    console.print("[green]✅ Authentication successful.[/green]\n")
                except Exception as e:
                    console.print(f"[red]❌ Authentication failed: {e}. Please try again.[/red]\n")

        # ── 4. Execution Lifecycle ───────────────────────────────────────────
        if mode_load or mode_export:
            db_context = AsyncSessionLocal()
            db_session = await db_context.__aenter__()

        progress = Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(bar_width=40), TaskProgressColumn(), TimeElapsedColumn())
        # Merge credentials into config if they exist
        config.update(state.to_config())
        if hasattr(state, "credentials") and state.credentials:
            config["username"], config["password"] = state.credentials

        with Live(Panel(progress, title="[bold]Synchronization Progress[/bold]", border_style="cyan", box=box.ROUNDED), console=console, refresh_per_second=4):
            if active_scrapers:
                master = progress.add_task("[bold blue]Downloading Data[/bold blue]", total=len(active_scrapers))
                try:
                    harvest = await asyncio.gather(*[run_step_async(s, PipelineContext(cas_client, db_session, progress, progress.add_task(f"  {s['name']}", total=None), config=config, log=lambda x: None)) for s in active_scrapers])
                    results.extend(harvest)
                except (asyncio.CancelledError, KeyboardInterrupt): pass
                progress.update(master, completed=len(active_scrapers))
            if active_loaders:
                master = progress.add_task("[bold magenta]Updating Database[/bold magenta]", total=len(active_loaders))
                for l in active_loaders:
                    res = await run_step_async(l, PipelineContext(cas_client, db_session, progress, progress.add_task(f"  {l['name']}", total=None), config=config))
                    results.append(res); progress.update(master, advance=1)
                    if res["status"] in ("❌ Failed", "🛑 Aborted"): break 
        
        if db_session:
            if not any(r["status"] in ("❌ Failed", "🛑 Aborted") for r in results):
                await db_session.commit()
                console.print("[bold green]✅ Database updates committed successfully.[/bold green]")
            else:
                await db_session.rollback()
                console.print("[bold red]🛑 Changes rolled back due to pipeline errors.[/bold red]")
        if db_context: await db_context.__aexit__(None, None, None)

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[bold yellow]⚠ OPERATION INTERRUPTED.[/bold yellow]")
        if db_session: await db_session.rollback()
        if db_context: await db_context.__aexit__(None, None, None)

    if results:
        console.print()
        table = Table(title="Summary", box=box.ROUNDED, border_style="blue")
        table.add_column("Step", style="white", min_width=30); table.add_column("Status", justify="center"); table.add_column("Duration", style="dim")
        for r in results:
            style = "green" if "Done" in r["status"] else "yellow" if "Aborted" in r["status"] else "red"
            table.add_row(r["name"], f"[{style}]{r['status']}[/{style}]", r.get("reason", "N/A"))
        console.print(table)

if __name__ == "__main__":
    asyncio.run(run_pipeline())
