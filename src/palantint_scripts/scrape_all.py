#!/usr/bin/env python3
"""
PalantINT — Master ETL Pipeline
===============================
Orchestrates the Extract, Transform, Load (ETL) data-ingestion sequence.
"""

import asyncio
import importlib
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

from casint import CASClient
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.table import Table
from rich import box
from rich.prompt import Prompt
from sqlalchemy.ext.asyncio import AsyncSession
from db.database import AsyncSessionLocal, engine

console = Console()

@dataclass
class PipelineContext:
    cas_client: Optional[CASClient]
    db_session: Optional[AsyncSession]
    progress: Progress
    task_id: int
    hydrate: bool = False
    delay: float = 0.2

# ── Step Definitions ───────────────────────────────────────────────────────

EXTRACT_STEPS = [
    {
        "name": "Extract Clubs",
        "description": "Scrape BDE club listings to JSON",
        "module": "palantint_scripts.scrape_clubs", # We'll adapt scrape_clubs to extract/load later or keep as is if no CAS needed
        "func": "scrape_clubs", # TODO: split clubs if needed, for now we will assume it just does direct DB write since no CAS is needed
        "needs_cas": False,
        "needs_db": True
    },
    {
        "name": "Extract TrombINT",
        "description": "Download student directory via CAS to JSON",
        "module": "palantint_scripts.extractors.trombint",
        "func": "extract_trombint",
        "needs_cas": True,
        "needs_db": False
    },
    {
        "name": "Extract Backfill",
        "description": "Search criteria to tag students to JSON",
        "module": "palantint_scripts.extractors.backfill",
        "func": "extract_backfill",
        "needs_cas": True,
        "needs_db": False
    },
    {
        "name": "Extract Agenda",
        "description": "Fetch emplois_du_temps to JSON",
        "module": "palantint_scripts.extractors.agenda",
        "func": "extract_agenda",
        "needs_cas": True,
        "needs_db": False
    }
]

LOAD_STEPS = [
    {
        "name": "Purge Database",
        "description": "Drop all tables and re-create schema",
        "module": None,
        "needs_cas": False,
        "needs_db": True,
        "condition": "purge_requested"
    },
    {
        "name": "Seed Relationships",
        "description": "Insert default relationship labels",
        "module": "palantint_scripts.populate",
        "func": "default_relationships",
        "needs_cas": False,
        "needs_db": True
    },
    {
        "name": "Load TrombINT",
        "description": "Sync student directory JSON to DB",
        "module": "palantint_scripts.loaders.trombint",
        "func": "load_trombint",
        "needs_cas": False,
        "needs_db": True
    },
    {
        "name": "Load Backfill",
        "description": "Apply tags from JSON to DB",
        "module": "palantint_scripts.loaders.backfill",
        "func": "load_backfill",
        "needs_cas": False,
        "needs_db": True
    },
    {
        "name": "Load Agenda",
        "description": "Sync agenda JSON to DB",
        "module": "palantint_scripts.loaders.agenda",
        "func": "load_agenda",
        "needs_cas": False,
        "needs_db": True
    },
    {
        "name": "Fix Missing Images",
        "description": "Re-download any missing profile photos",
        "module": "palantint_scripts.fix_images", # Requires both DB (to know who) and CAS (to download)
        "func": "fix_missing_images",
        "needs_cas": True,
        "needs_db": True
    }
]

async def run_purge_db():
    from db.models import SQLModel
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

async def purge_json():
    import shutil
    scrap_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../data/scraps"))
    if os.path.exists(scrap_dir):
        shutil.rmtree(scrap_dir)
        os.makedirs(scrap_dir, exist_ok=True)
        console.print("[green]✅ JSON Data Lake purged.[/green]")

async def run_pipeline():
    cas_username = os.getenv("CAS_USERNAME", "")
    cas_password = os.getenv("CAS_PASSWORD", "")
    
    # Check flags
    purge_requested = "--purge" in sys.argv
    hydrate_requested = "--hydrate" in sys.argv
    mode_extract = "--extract" in sys.argv
    mode_load = "--load" in sys.argv
    
    console.print()
    console.print(Panel.fit(
        "[bold white]PalantINT[/bold white] [blue]ETL Data Pipeline[/blue]\n"
        "[dim]Extract (Scrape to JSON) -> Load (Sync to DB)[/dim]",
        border_style="blue",
        box=box.HEAVY,
    ))
    console.print()

    if "--purge-json" in sys.argv:
        await purge_json()
        if len(sys.argv) == 2: return # Exit if only command

    # Interactive Setup if no flags provided
    if not (mode_extract or mode_load or purge_requested or hydrate_requested):
        console.print("[bold]Pipeline Configuration[/bold]")
        
        import questionary
        
        phase_choice = questionary.select(
            "1. Select Pipeline Phase:",
            choices=[
                questionary.Choice("Full Sync (Extract & Load)", value="full_sync"),
                questionary.Choice("Extract Only (Scrape to JSON)", value="extract_only"),
                questionary.Choice("Load Only (Sync JSON to Database)", value="load_only")
            ]
        ).ask()
        
        if phase_choice == "extract_only":
            mode_extract = True
            mode_load = False
        elif phase_choice == "load_only":
            mode_extract = False
            mode_load = True
        else:
            mode_extract = True
            mode_load = True
            
        if mode_load:
            db_mode = questionary.select(
                "2. Select Database Mode:",
                choices=[
                    questionary.Choice("Incremental (Only new/missing data)", value="incremental"),
                    questionary.Choice("Hydrate (Force re-verify all records)", value="hydrate"),
                    questionary.Choice("Purge (Wipe database before loading)", value="purge")
                ]
            ).ask()
            
            if db_mode == "purge":
                purge_requested = True
            elif db_mode == "hydrate":
                hydrate_requested = True

    if not mode_extract and not mode_load:
        console.print("[yellow]No phases selected. Exiting.[/yellow]")
        return

    if purge_requested:
        console.print("\n[bold red]⚠  PURGE MODE ENABLED.[/bold red] Database will be wiped before loading.\n")
    elif hydrate_requested:
        console.print("\n[bold cyan]💧 HYDRATION MODE.[/bold cyan] Full re-verification of all records enabled.\n")
    else:
        console.print("\n[bold green]ℹ  INCREMENTAL MODE.[/bold green] Only new or missing data will be processed.\n")

    import questionary
    speed_mode = questionary.select(
        "Select Scraping Speed:",
        choices=[
            questionary.Choice("Normal (0.2s delay - Recommended)", value="normal"),
            questionary.Choice("Stealth (1.0s delay - Safest)", value="stealth"),
            questionary.Choice("Aggressive (0.0s delay - Fastest)", value="aggressive")
        ]
    ).ask()
    
    speed_delays = {"stealth": 1.0, "normal": 0.2, "aggressive": 0.0}
    delay = speed_delays[speed_mode]
    console.print(f"[dim]Speed set to: {speed_mode.upper()} ({delay}s delay)[/dim]\n")

    # Combine steps based on mode
    active_steps = []
    if mode_extract: active_steps.extend(EXTRACT_STEPS)
    if mode_load: active_steps.extend(LOAD_STEPS)

    needs_any_cas = any(s.get("needs_cas", False) for s in active_steps)
    needs_any_db = any(s.get("needs_db", False) for s in active_steps)
    
    cas_client = None
    if needs_any_cas:
        if not cas_username:
            cas_username = questionary.text("Enter CAS Username:").ask()
        if not cas_password:
            cas_password = questionary.password("Enter CAS Password:").ask()

        if cas_username and cas_password:
            console.print("\n[blue]ℹ  Authenticating with CAS...[/blue]")
            try:
                cas_client = CASClient(service_url="https://cas6.imtbs-tsp.eu/cas/login")
                await cas_client.login(username=cas_username, password=cas_password)
                CASClient.set_shared_instance(cas_client)
                console.print("[green]✅ CAS authentication successful.[/green]\n")
            except Exception as e:
                console.print(f"[red]❌ CAS authentication failed: {e}[/red]\n")
                cas_client = None
        else:
            console.print("\n[yellow]⚠  No CAS credentials provided. CAS steps will be skipped.[/yellow]\n")

    db_session = None
    if needs_any_db:
        try:
            db_context = AsyncSessionLocal()
            db_session = await db_context.__aenter__()
        except Exception as e:
            console.print(f"[red]❌ Database connection failed. Is the Docker container running? Error: {e}[/red]\n")
            needs_any_db = False # Cancel DB steps

    results = []
    total_start = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        master_task = progress.add_task("[bold blue]Pipeline", total=len(active_steps))

        for step in active_steps:
            step_name = step["name"]
            
            if step.get("condition") == "purge_requested" and not purge_requested:
                results.append({"name": step_name, "status": "⏭️  Skipped", "reason": "No --purge flag"})
                progress.update(master_task, advance=1)
                continue

            if step.get("needs_cas") and not cas_client:
                results.append({"name": step_name, "status": "⏭️  Skipped", "reason": "No CAS credentials"})
                progress.update(master_task, advance=1)
                continue
                
            if step.get("needs_db") and not db_session:
                results.append({"name": step_name, "status": "⏭️  Skipped", "reason": "No DB connection"})
                progress.update(master_task, advance=1)
                continue

            step_task = progress.add_task(f"  {step_name}", total=1)
            start = time.time()

            try:
                if step["module"] is None:
                    await run_purge_db()
                else:
                    mod = importlib.import_module(step["module"])
                    func = getattr(mod, step["func"])
                    
                    import inspect
                    sig = inspect.signature(func)
                    params = list(sig.parameters.keys())

                    kwargs = {}
                    if "context" in params:
                        kwargs["context"] = PipelineContext(
                            cas_client=cas_client,
                            db_session=db_session,
                            progress=progress,
                            task_id=step_task,
                            hydrate=hydrate_requested,
                            delay=delay
                        )
                    else:
                        if "cas_client" in params: kwargs["cas_client"] = cas_client
                        if "db_session" in params: kwargs["db_session"] = db_session
                        if "session" in params: kwargs["session"] = db_session
                        if "progress" in params: kwargs["progress"] = progress
                        if "task_id" in params: kwargs["task_id"] = step_task
                        if "delay" in params: kwargs["delay"] = delay
                    
                    if "force_reverify" in params:
                        kwargs["force_reverify"] = hydrate_requested

                    await func(**kwargs)

                elapsed = time.time() - start
                results.append({"name": step_name, "status": "✅ Done", "reason": f"{elapsed:.1f}s"})
                progress.update(step_task, advance=1)

            except Exception as e:
                elapsed = time.time() - start
                results.append({"name": step_name, "status": "❌ Failed", "reason": str(e)[:60]})
                progress.update(step_task, advance=1)
                console.print(f"  [red]Error in {step_name}: {e}[/red]")

            progress.update(master_task, advance=1)
    
    if db_session:
        await db_session.commit()
        await db_context.__aexit__(None, None, None)

    total_elapsed = time.time() - total_start
    console.print()
    table = Table(title="ETL Pipeline Summary", box=box.ROUNDED, border_style="blue")
    table.add_column("Step", style="white", min_width=25)
    table.add_column("Status", justify="center")
    table.add_column("Details", style="dim")

    for r in results:
        status_style = "green" if "Done" in r["status"] else "yellow" if "Skipped" in r["status"] else "red"
        table.add_row(r["name"], f"[{status_style}]{r['status']}[/{status_style}]", r["reason"])

    console.print(table)
    console.print(f"\n[bold]Total time:[/bold] {total_elapsed:.1f}s\n")

def main():
    try:
        asyncio.run(run_pipeline())
    except KeyboardInterrupt:
        console.print("\n[bold red]Pipeline interrupted by user.[/bold red]")

if __name__ == "__main__":
    main()
