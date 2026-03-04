#!/usr/bin/env python3
"""
PalantINT — Master Scraping Pipeline
=====================================
Orchestrates the full data-ingestion sequence with real-time Rich progress tracking.

Usage:
  uv run --with rich --with httpx --with beautifulsoup4 --with requests --with unidecode python scripts/scrape_all.py

Environment Variables:
  CAS_USERNAME  — CAS login username (required for TrombINT + Backfill + Fix Images)
  CAS_PASSWORD  — CAS login password (required for TrombINT + Backfill + Fix Images)
  DATABASE_URL  — Postgres connection string (defaults to local Docker instance)
"""

import asyncio
import importlib
import os
import sys
import time

# Ensure the backend is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend/src')))

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn
from rich.table import Table
from rich import box

console = Console()

STEPS = [
    {
        "name": "Purge Database",
        "description": "Drop all tables and re-create schema",
        "module": None,  # Handled inline
    },
    {
        "name": "Seed Relationship Types",
        "description": "Insert default relationship labels (Amis, En couple, Ex)",
        "module": "palantint_scripts.populate",
        "func": "default_relationships",
    },
    {
        "name": "Scrape Clubs",
        "description": "Fetch BDE club listings",
        "module": "palantint_scripts.scrape_clubs",
        "func": "scrape_clubs",
    },
    {
        "name": "Scrape TrombINT",
        "description": "Download student directory via CAS",
        "module": "palantint_scripts.scrape_trombint",
        "func": "scrape_trombint",
        "needs_cas": True,
    },
    {
        "name": "Backfill Students",
        "description": "Enrich students with école/filière data",
        "module": "palantint_scripts.backfill",
        "func": "main",
        "needs_cas": True,
    },
    {
        "name": "Import Agenda",
        "description": "Ingest emplois_du_temps JSON scraps into DB",
        "module": "palantint_scripts.import_agenda",
        "func": "main",
    },
    {
        "name": "Fix Missing Images",
        "description": "Re-download any missing profile photos",
        "module": "palantint_scripts.fix_images",
        "func": "main",
        "needs_cas": True,
    },
]

# ── Utilities ───────────────────────────────────────────────────────────────

async def run_purge_db():
    """Drop all tables and re-create schema."""
    from sqlmodel import SQLModel
    from db.database import engine
    from db import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


def load_step_function(step: dict):
    """Dynamically import a module and return the target async function."""
    module_path = step["module"]
    func_name = step["func"]

    mod = importlib.import_module(module_path)
    return getattr(mod, func_name)


# ── Main Pipeline ───────────────────────────────────────────────────────────

async def run_pipeline():
    cas_username = os.getenv("CAS_USERNAME", "")
    cas_password = os.getenv("CAS_PASSWORD", "")

    purge_requested = "--purge" in sys.argv

    # Header
    console.print()
    console.print(Panel.fit(
        "[bold white]PalantINT[/bold white] [blue]Data Pipeline[/blue]\n"
        "[dim]Autonomous scraping & database synchronization[/dim]",
        border_style="blue",
        box=box.HEAVY,
    ))
    console.print()

    if purge_requested:
        console.print("[bold red]⚠  PURGE MODE ENABLED.[/bold red] Database will be wiped before scraping.\n")
    else:
        console.print("[bold green]ℹ  INCREMENTAL MODE.[/bold green] Existing records will be updated, not deleted.\n")

    # Credential check
    if not cas_username or not cas_password:
        console.print("[yellow]⚠  CAS_USERNAME / CAS_PASSWORD not set.[/yellow]")
        console.print("[dim]   Steps requiring CAS authentication will be skipped.[/dim]\n")

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
        master_task = progress.add_task("[bold blue]Pipeline", total=len(STEPS))

        for i, step in enumerate(STEPS):
            step_name = step["name"]
            step_desc = step["description"]
            needs_cas = step.get("needs_cas", False)

            # Handle Purge Step skipping
            if step["module"] is None and not purge_requested:
                results.append({"name": step_name, "status": "⏭️  Skipped", "reason": "No --purge flag"})
                progress.update(master_task, advance=1)
                continue

            # Skip CAS-dependent steps if no credentials
            if needs_cas and (not cas_username or not cas_password):
                results.append({"name": step_name, "status": "⏭️  Skipped", "reason": "No CAS credentials"})
                progress.update(master_task, advance=1)
                continue

            step_task = progress.add_task(f"  {step_name}", total=1)
            start = time.time()

            try:
                if step["module"] is None:
                    # Inline purge
                    await run_purge_db()
                else:
                    func = load_step_function(step)

                    # Determine how to call the function based on its signature
                    import inspect
                    sig = inspect.signature(func)
                    params = list(sig.parameters.keys())

                    if "cas_username" in params and "cas_password" in params:
                        await func(cas_username, cas_password)
                    else:
                        result = func()
                        if asyncio.iscoroutine(result):
                            await result

                elapsed = time.time() - start
                results.append({"name": step_name, "status": "✅ Done", "reason": f"{elapsed:.1f}s"})
                progress.update(step_task, advance=1)

            except Exception as e:
                elapsed = time.time() - start
                results.append({"name": step_name, "status": "❌ Failed", "reason": str(e)[:60]})
                progress.update(step_task, advance=1)
                console.print(f"  [red]Error in {step_name}: {e}[/red]")

            progress.update(master_task, advance=1)

    total_elapsed = time.time() - total_start

    # Summary table
    console.print()
    table = Table(title="Pipeline Summary", box=box.ROUNDED, border_style="blue")
    table.add_column("Step", style="white", min_width=25)
    table.add_column("Status", justify="center")
    table.add_column("Details", style="dim")

    for r in results:
        status_style = "green" if "Done" in r["status"] else "yellow" if "Skipped" in r["status"] else "red"
        table.add_row(r["name"], f"[{status_style}]{r['status']}[/{status_style}]", r["reason"])

    console.print(table)
    console.print(f"\n[bold]Total time:[/bold] {total_elapsed:.1f}s\n")


def main():
    """CLI Entry point."""
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()
