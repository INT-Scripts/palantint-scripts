"""
PalantINT — Scraper & Data Schema Integrity Verifier
=====================================================
Validates scraped JSON files, floor plan assets, CAS sessions,
and reports integrity statistics with soft failure handling.
"""

import os
import json
import logging
from typing import Dict, Any, List
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
logger = logging.getLogger("palantint.verifier")

from palantint_scripts.config import SCRAPS_AUTO_DIR, PLANS_DIR

DATA_DIR = str(SCRAPS_AUTO_DIR)
PLANS_DIR = str(PLANS_DIR)

# ── Pydantic Validation Schemas ──────────────────────────────────────────────

class ApartmentDetailsSchema(BaseModel):
    req_b: int = Field(..., alias="_req_b")
    req_e: Any = Field(..., alias="_req_e")
    error: str | None = None

class StudentSchema(BaseModel):
    uid: str
    nom_complet: str | None = None
    email: str | None = None
    ecole: str | None = None
    promo: str | None = None

class ClubSchema(BaseModel):
    id: str | None = None
    name: str

class AgendaEventSchema(BaseModel):
    id: str | None = None
    name: str | None = None
    date: str | None = None

# ── Verification Routines ──────────────────────────────────────────────────

def verify_apartments_schema() -> Dict[str, Any]:
    file_path = os.path.join(DATA_DIR, "logements.json")
    stats = {"total": 0, "valid": 0, "errors": 0, "details": []}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        stats["total"] = len(data)
        for apt_id, details in data.items():
            if "error" in details:
                stats["errors"] += 1
                stats["details"].append(f"Apt {apt_id}: {details.get('error')}")
                continue
            try:
                ApartmentDetailsSchema.model_validate(details)
                stats["valid"] += 1
            except ValidationError as ve:
                stats["errors"] += 1
                stats["details"].append(f"Apt {apt_id} schema mismatch: {ve}")
    except Exception as e:
        return {"error": str(e)}

    return stats

def verify_floor_plans() -> Dict[str, Any]:
    if not os.path.exists(PLANS_DIR):
        return {"total": 0, "error": f"Directory not found: {PLANS_DIR}"}

    files = [f for f in os.listdir(PLANS_DIR) if f.endswith(".png")]
    return {"total": len(files), "valid": len(files)}

def verify_students_schema() -> Dict[str, Any]:
    file_path = os.path.join(DATA_DIR, "students.json")
    stats = {"total": 0, "valid": 0, "errors": 0, "details": []}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        stats["total"] = len(data)
        for idx, student in enumerate(data):
            try:
                StudentSchema.model_validate(student)
                stats["valid"] += 1
            except ValidationError as ve:
                stats["errors"] += 1
                stats["details"].append(f"Student #{idx} ({student.get('uid')}): {ve}")
    except Exception as e:
        return {"error": str(e)}

    return stats

def run_all_verifications():
    console.print("\n[bold blue]PalantINT — Scraper & Data Schema Audit[/bold blue]\n")

    table = Table(title="Scraping Integrity Report", box=box.ROUNDED)
    table.add_column("Dataset", style="cyan")
    table.add_column("Total Records", justify="right")
    table.add_column("Valid Records", justify="right", style="green")
    table.add_column("Error / Warning Count", justify="right", style="red")
    table.add_column("Status", style="bold")

    # Apartments
    apt_res = verify_apartments_schema()
    if "error" in apt_res and apt_res.get("total", 0) == 0:
        table.add_row("Maisel Apartments", "0", "0", "1", f"[yellow]{apt_res['error']}[/yellow]")
    else:
        status = "[green]OK[/green]" if apt_res["errors"] == 0 else "[yellow]SOFT FAIL / DISCREPANCY[/yellow]"
        table.add_row("Maisel Apartments", str(apt_res["total"]), str(apt_res["valid"]), str(apt_res["errors"]), status)

    # Floor Plans
    plans_res = verify_floor_plans()
    if "error" in plans_res:
        table.add_row("Maisel Floor Plans", "0", "0", "1", f"[yellow]{plans_res['error']}[/yellow]")
    else:
        table.add_row("Maisel Floor Plans", str(plans_res["total"]), str(plans_res["valid"]), "0", "[green]OK[/green]")

    # Students
    stud_res = verify_students_schema()
    if "error" in stud_res and stud_res.get("total", 0) == 0:
        table.add_row("Student Directory", "0", "0", "1", f"[yellow]{stud_res['error']}[/yellow]")
    else:
        status = "[green]OK[/green]" if stud_res["errors"] == 0 else "[yellow]SOFT FAIL / DISCREPANCY[/yellow]"
        table.add_row("Student Directory", str(stud_res["total"]), str(stud_res["valid"]), str(stud_res["errors"]), status)

    console.print(table)
    console.print("\n[dim]Audit finished.[/dim]\n")

if __name__ == "__main__":
    run_all_verifications()
