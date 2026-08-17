"""Harvest the full course catalogs of the three schools into a single JSON.

Two distinct notions, discovered while exploring the catalogs:

* Provenance (`ecoles` field): which school's catalog page LISTS the course.
  The three listings (tsp / imt-bs / lsh) are almost disjoint — a course
  usually appears in only ONE of them (the field stays a list to cover the
  rare "several" case).

* Content (the rest of the fiche): the same id queried under the three fiche
  endpoints does not return the same completeness (a field filled under tsp
  can be empty under imt-bs...). Each course is therefore fetched under the
  three schools and MERGED via the library's Course.merge() — richest fiche.

Everything is requested in French (lang="fr"). intllabus is a synchronous
(requests) SDK, so fiches are fetched through asyncio.to_thread with one
client per school and per thread (requests.Session is not thread-safe).
Failures are collected (id + reason) and written to the output file so the
missed courses can be retried.

Output: data/scraps/auto/courses.json
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from intllabus import Course, IntllabusClient

from palantint_scripts.config import SCRAPS_AUTO_DIR

OUTPUT_FILE = SCRAPS_AUTO_DIR / "courses.json"

SCHOOLS = ["tsp", "imt-bs", "lsh"]
CHECKPOINT_EVERY = 300  # intermediate save every N courses

# One client per school and per thread (requests.Session is not thread-safe).
_local = threading.local()


def _clients() -> Dict[str, IntllabusClient]:
    clients = getattr(_local, "clients", None)
    if clients is None:
        clients = {school: IntllabusClient(school) for school in SCHOOLS}
        _local.clients = clients
    return clients


def _is_real(course: Course) -> bool:
    """Same rule as the library: a fiche is empty if the title is unknown AND
    it carries no content at all."""
    return not (
        course.title == "Titre Inconnu"
        and not course.description
        and not course.objectif
    )


def _merged_course(course_id: str) -> Optional[Course]:
    """Fiche of `course_id` merged across the three schools."""
    clients = _clients()
    merged: Optional[Course] = None
    for school in SCHOOLS:
        try:
            course = clients[school].get_course(course_id, lang="fr")
        except Exception:  # noqa: BLE001 — one school being down must not stop the others
            continue
        if _is_real(course):
            merged = course if merged is None else merged.merge(course)
    return merged


def _save(
    courses: List[Dict[str, Any]],
    listing_counts: Dict[str, int],
    errors: List[Dict[str, str]],
) -> None:
    ordered = sorted(courses, key=lambda c: c["id"])
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "total": len(ordered),
            "par_ecole_listing": listing_counts,
            "multi_ecoles": sum(1 for c in ordered if len(c["ecoles"]) > 1),
            "errors": len(errors),
        },
        "courses": ordered,
        # Each error = {id, error}: enough to retry only the failed courses.
        "errors": errors,
    }
    OUTPUT_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_existing() -> Dict[str, Dict[str, Any]]:
    """Previously scraped fiches, keyed by course id (incremental mode)."""
    if not OUTPUT_FILE.exists():
        return {}
    try:
        payload = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {c["id"]: c for c in payload.get("courses", []) if c.get("id")}


async def scrape_courses(progress=None, task_id=None, config: dict = None, log=print):
    """Standardized entry point using the shared config dict.

    config keys honoured: `concurrency` (parallel fiches), `delay` (cooldown
    between fiches, per worker) and `full_sync` (re-download fiches already
    present in courses.json instead of keeping them).
    """
    config = config or {}
    concurrency = max(1, int(config.get("concurrency", 5)))
    delay = float(config.get("delay", 0.2))
    full_sync = bool(config.get("full_sync", False))

    def _update(description: str, **kwargs):
        if progress and task_id is not None:
            progress.update(task_id, description=description, **kwargs)

    # ── Phase 1: per-school listings -> provenance ───────────────────────────
    _update("  [blue]Scraping Courses: Listing catalogs...[/blue]")
    log("Listing the course catalogs...")

    listings: Dict[str, set] = {}
    for school in SCHOOLS:
        try:
            summaries = await asyncio.to_thread(
                lambda s=school: IntllabusClient(s).get_all_courses(lang="fr")
            )
        except Exception as e:  # noqa: BLE001
            log(f"[yellow]Catalog listing failed for {school}: {e}[/yellow]")
            listings[school] = set()
            continue
        listings[school] = {c.id for c in summaries}
        log(f"  [cyan]{school}[/cyan]: {len(listings[school])} courses listed")

    all_ids = sorted(set().union(*listings.values())) if listings else []
    if not all_ids:
        raise RuntimeError("No course could be listed — catalogs unreachable?")

    listing_counts = {school: len(ids) for school, ids in listings.items()}
    ecoles_of = {
        cid: [school for school in SCHOOLS if cid in listings[school]] for cid in all_ids
    }
    log(f"  union: [cyan]{len(all_ids)}[/cyan] unique courses")

    # ── Phase 2: merged fiche of every course, in parallel ───────────────────
    existing = {} if full_sync else _load_existing()
    todo = [cid for cid in all_ids if cid not in existing]

    # Keep the fiches already harvested, refreshing their provenance.
    courses: List[Dict[str, Any]] = []
    for cid in all_ids:
        if cid in existing:
            kept = existing[cid]
            kept["ecoles"] = ecoles_of[cid]
            courses.append(kept)

    if not todo:
        _save(courses, listing_counts, [])
        _update(
            f"  [green]Scraping Courses: Up to date ({len(courses)} courses).[/green]",
            completed=1,
            total=1,
        )
        log(f"[green]Catalog already up to date ({len(courses)} courses).[/green]")
        return

    log(
        f"Fetching [cyan]{len(todo)}[/cyan] course sheets "
        f"({concurrency} in parallel, {len(courses)} kept from the previous run)..."
    )
    _update(
        "  [blue]Scraping Courses: Fetching course sheets...[/blue]",
        total=len(todo),
        completed=0,
    )

    errors: List[Dict[str, str]] = []
    semaphore = asyncio.Semaphore(concurrency)
    done = 0
    lock = asyncio.Lock()

    async def fetch_one(cid: str):
        nonlocal done
        async with semaphore:
            try:
                merged = await asyncio.to_thread(_merged_course, cid)
            except Exception as e:  # noqa: BLE001
                merged = None
                async with lock:
                    errors.append({"id": cid, "error": str(e)})
            else:
                if merged is None:
                    async with lock:
                        errors.append({"id": cid, "error": "no usable course sheet"})
                else:
                    data = asdict(merged)
                    data["ecoles"] = ecoles_of[cid]
                    async with lock:
                        courses.append(data)
            if delay > 0:
                await asyncio.sleep(delay)

        async with lock:
            done += 1
            if progress and task_id is not None:
                progress.update(task_id, advance=1)
            if done % CHECKPOINT_EVERY == 0:
                _save(courses, listing_counts, errors)
                log(f"  {done}/{len(todo)} course sheets (checkpoint)")

    try:
        await asyncio.gather(*(fetch_one(cid) for cid in todo))
    finally:
        # Even on Ctrl-C, whatever was harvested stays on disk.
        _save(courses, listing_counts, errors)

    log(f"[green]{len(courses)} courses -> {OUTPUT_FILE.name}[/green]")

    if errors:
        log(f"[yellow]{len(errors)} courses FAILED (see the 'errors' field):[/yellow]")
        for err in errors[:20]:
            log(f"  - {err['id']}: {err['error']}")
        _update(
            f"  [yellow]Scraping Courses: Done with {len(errors)} failures "
            f"({len(courses)} courses).[/yellow]"
        )
    else:
        _update(f"  [green]Scraping Courses: Done ({len(courses)} courses).[/green]")


async def main():
    await scrape_courses(config={"delay": 0.1, "concurrency": 8})


if __name__ == "__main__":
    asyncio.run(main())
