#!/usr/bin/env python3
"""
PalantINT — Data Synchronization Pipeline
===============================
Orchestrates the scraping and loading of campus data.
"""

import asyncio
import importlib
import os
import sys
import time
import questionary
from dataclasses import dataclass
from typing import Optional, Callable, List

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

custom_style = Style([
    ('qmark', 'fg:#5fafff bold'), ('question', 'bold'), ('answer', 'fg:#5fafff bold'),
    ('pointer', 'fg:#5fafff bold'), ('highlighted', 'fg:#ffffff bg:#005faf bold'), 
    ('selected', 'fg:#5fafff'), ('separator', 'fg:#6c6c6c'), ('instruction', 'fg:#8a8a8a italic'),
])

@dataclass
class PipelineContext:
    cas_client: Optional[CASClient]
    db_session: Optional[AsyncSession]
    progress: Progress
    task_id: int
    hydrate: bool = True
    delay: float = 0.2
    concurrency: int = 5
    log: Callable[[str], None] = lambda x: None
    agenda_mode: str = "all"
    agenda_custom_id: Optional[str] = None

PIPELINE_DOMAINS = [
    {"id": "clubs", "name": "Clubs & Organizations", "scraper": {"name": "Scraping Clubs", "module": "palantint_scripts.scrapers.clubs", "func": "scrape_clubs"}, "loader": {"name": "Update Clubs", "module": "palantint_scripts.loaders.clubs", "func": "load_clubs", "needs_db": True}},
    {"id": "students", "name": "Student Directory", "scraper": {"name": "Scraping Students", "module": "palantint_scripts.scrapers.trombint", "func": "scrape_trombint", "needs_cas": True}, "loader": {"name": "Update Students", "module": "palantint_scripts.loaders.trombint", "func": "load_trombint", "needs_db": True}},
    {"id": "agenda", "name": "Timetables", "scraper": {"name": "Scraping Schedules", "module": "palantint_scripts.scrapers.agenda", "func": "scrape_agenda", "needs_cas": True}, "loader": {"name": "Update Schedules", "module": "palantint_scripts.loaders.agenda", "func": "load_agenda", "needs_db": True}},
    {"id": "media", "name": "Media Assets", "scraper": {"name": "Harvesting Media", "module": "palantint_scripts.scrapers.media", "func": "scrape_media", "needs_cas": True}},
    {"id": "apartments", "name": "Apartments", "loader": {"name": "Update Apartments", "module": "palantint_scripts.loaders.apartments", "func": "load_apartments", "needs_db": True}}
]

async def run_step_async(step, ctx: PipelineContext):
    start = time.time()
    try:
        if step.get("module") is None:
            if step["name"] == "Wipe Database":
                from db.models import SQLModel
                async with engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.drop_all)
                    await conn.run_sync(SQLModel.metadata.create_all)
            elif step["name"] == "Export Database":
                from .exporter import export_db_data
                await export_db_data(ctx.db_session, lambda x: None)
        else:
            mod = importlib.import_module(step["module"])
            func = getattr(mod, step["func"])
            import inspect
            sig = inspect.signature(func)
            kwargs = {k: v for k, v in {
                "context": ctx, "cas_client": ctx.cas_client, "db_session": ctx.db_session, "session": ctx.db_session,
                "progress": ctx.progress, "task_id": ctx.task_id, "delay": ctx.delay, "concurrency": ctx.concurrency, "log": ctx.log, "force_reverify": ctx.hydrate
            }.items() if k in sig.parameters}
            await func(**kwargs)

        ctx.progress.update(ctx.task_id, description=f"[green]  {step['name']}: Done.[/green]", completed=1, total=1)
        return {"name": step["name"], "status": "✅ Done", "reason": f"{time.time()-start:.1f}s"}
    except (asyncio.CancelledError, KeyboardInterrupt):
        ctx.progress.update(ctx.task_id, description=f"[yellow]  {step['name']}: Interrupted.[/yellow]", completed=1, total=1)
        return {"name": step["name"], "status": "🛑 Aborted", "reason": "Progress Saved"}
    except Exception as e:
        ctx.progress.update(ctx.task_id, description=f"[red]  {step['name']}: Failed![/red]", completed=1, total=1)
        return {"name": step["name"], "status": "❌ Failed", "reason": str(e)[:60]}

async def run_pipeline():
    db_session, db_context, cas_client = None, None, None
    results = []
    try:
        console.print(Panel.fit("[bold white]PalantINT[/bold white] [blue]Data Synchronization[/blue]\n[dim]Synchronizing registry identities and OSINT vaults.[/dim]", border_style="blue", box=box.HEAVY))
        
        # ── 1. Selection Phase ───────────────────────────────────────────────
        phase_choice = await questionary.select("Select Operation:", choices=[
            questionary.Choice("Download External Data (Web -> JSON)", value="scrape_only"),
            questionary.Choice("Create Backup (DB -> JSON)", value="export_only"),
            questionary.Choice("Update Database (JSON -> DB)", value="load_only"),
        ], style=custom_style).ask_async()
        if not phase_choice: return

        mode_scrape, mode_load, mode_export = phase_choice == "scrape_only", phase_choice == "load_only", phase_choice == "export_only"
        purge_requested, delay, concurrency = False, 0.2, 5
        agenda_mode, agenda_custom_id = "all", None

        if mode_load:
            db_strat = await questionary.select("Database Strategy:", choices=[
                questionary.Choice("Standard Update (Synchronize all records)", value="update"),
                questionary.Choice("Clean Reset (Wipe database before updating)", value="purge")
            ], style=custom_style).ask_async()
            if not db_strat: return
            purge_requested = db_strat == "purge"

        selected_ids = await questionary.checkbox("Select Data to Process:", choices=[
            questionary.Choice(d["name"], value=d["id"], checked=True) for d in PIPELINE_DOMAINS
        ], style=custom_style).ask_async() if mode_scrape or mode_load else []
        if (mode_scrape or mode_load) and not selected_ids: return

        if "agenda" in selected_ids and mode_scrape:
            agenda_mode = await questionary.select("Schedule Depth:", choices=[
                questionary.Choice("Standard (Next 3 months)", value="all"),
                questionary.Choice("Quick (Next 2 weeks only)", value="quick"),
                questionary.Choice("Targeted ID", value="specific")
            ], style=custom_style).ask_async()
            if agenda_mode == "specific":
                agenda_custom_id = await questionary.text("Enter Calendar ID:", style=custom_style).ask_async()

        if mode_scrape:
            speed = await questionary.select("Download Velocity:", choices=[
                questionary.Choice("Standard (0.2s cooldown, 5 connections)", value=(0.2, 5)),
                questionary.Choice("Safe (1.0s cooldown, 1 connection)", value=(1.0, 1)),
                questionary.Choice("Aggressive (0.0s cooldown, 10 connections)", value=(0.0, 10)),
                questionary.Choice("Custom (Manual parameters)", value="custom")
            ], style=custom_style).ask_async()
            if speed == "custom":
                delay = float(await questionary.text("Cooldown (seconds):", default="0.5", style=custom_style).ask_async() or 0.5)
                concurrency = int(await questionary.text("Simultaneous connections:", default="3", style=custom_style).ask_async() or 3)
            elif speed is not None: delay, concurrency = speed

        # ── 2. Task Mapping ──────────────────────────────────────────────────
        active_scrapers = [d["scraper"] for d in PIPELINE_DOMAINS if d["id"] in selected_ids and "scraper" in d] if mode_scrape else []
        active_loaders = []
        if mode_load:
            if purge_requested: active_loaders.append({"name": "Wipe Database", "module": None})
            active_loaders.append({"name": "Setup Infrastructure", "module": "db.seed", "func": "seed_default_data"})
            
            # PASS 1: Mandatory Identity Anchoring (UUID Stability)
            active_loaders.append({"name": "Anchor Identities", "module": "palantint_scripts.loaders.vault", "func": "anchor_identities"})
            
            # PASS 2: Synchronize Selected Scraps (Web Data Updates)
            active_loaders.extend([d["loader"] for d in PIPELINE_DOMAINS if d["id"] in selected_ids and "loader" in d])
            
            # PASS 3: Mandatory Research Restoration (OSINT Binding)
            active_loaders.append({"name": "Restore Research", "module": "palantint_scripts.loaders.vault", "func": "restore_research"})
        
        if mode_export: active_loaders.append({"name": "Export Database", "module": None})

        # ── 3. Authentication Phase ──────────────────────────────────────────
        needs_cas = any(s.get("needs_cas") for s in active_scrapers + active_loaders)
        if needs_cas:
            while not cas_client:
                user = await questionary.text("Enter Username:", style=custom_style).ask_async()
                if not user: raise KeyboardInterrupt
                pw = await questionary.password("Enter Password:", style=custom_style).ask_async()
                if not pw: raise KeyboardInterrupt
                console.print("\n[blue]ℹ Authenticating...[/blue]")
                try:
                    client = CASClient(service_url="https://cas6.imtbs-tsp.eu/cas/login")
                    await client.login(username=user, password=pw)
                    cas_client = client
                    console.print("[green]✅ Authentication successful.[/green]\n")
                except Exception as e:
                    console.print(f"[red]❌ Authentication failed: {e}. Please try again.[/red]\n")

        # ── 4. Execution Lifecycle ───────────────────────────────────────────
        if mode_load or mode_export:
            db_context = AsyncSessionLocal()
            db_session = await db_context.__aenter__()

        progress = Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), BarColumn(bar_width=40), TaskProgressColumn(), TimeElapsedColumn())
        with Live(Panel(progress, title="[bold]Synchronization Progress[/bold]", border_style="cyan", box=box.ROUNDED), console=console, refresh_per_second=4):
            if active_scrapers:
                master = progress.add_task("[bold blue]Downloading Data[/bold blue]", total=len(active_scrapers))
                try:
                    harvest = await asyncio.gather(*[run_step_async(s, PipelineContext(cas_client, db_session, progress, progress.add_task(f"  {s['name']}", total=None), delay=delay, concurrency=concurrency, agenda_mode=agenda_mode, agenda_custom_id=agenda_custom_id)) for s in active_scrapers])
                    results.extend(harvest)
                except (asyncio.CancelledError, KeyboardInterrupt): pass
                progress.update(master, completed=len(active_scrapers))
            if active_loaders:
                master = progress.add_task("[bold magenta]Updating Database[/bold magenta]", total=len(active_loaders))
                for l in active_loaders:
                    res = await run_step_async(l, PipelineContext(cas_client, db_session, progress, progress.add_task(f"  {l['name']}", total=None)))
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
