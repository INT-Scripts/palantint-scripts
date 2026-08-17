"""Hydrate the `courses` table from data/scraps/auto/courses.json.

The scrape is keyed on the catalog id (`Course.external_id`), not on the course
code: 7 codes are shared by two different fiches in the published catalog.
Courses that disappear from the catalog are deactivated (`is_active = False`)
rather than deleted, per the schema's lifecycle rules.

Nothing is linked to Person here: teacher names stay raw JSON on the course.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import Course, CourseTeacher
from db.naming import person_name_key

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import (
    finish_ingestion_run,
    get_data_source_id,
    start_ingestion_run,
)

JSON_PATH = SCRAPS_AUTO_DIR / "courses.json"

SOURCE_CODE = "intllabus"

# Course.merge() joins values that disagree between two schools' fiches with
# this separator; scalar columns keep the first of them.
MERGE_SEPARATOR = "\n\n---\n\n"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _first(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).split(MERGE_SEPARATOR)[0].strip()
    return value or None


def _text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _number(raw: Any) -> Optional[float]:
    """'2,5' -> 2.5. Returns None for anything non-numeric."""
    value = _first(raw)
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _teacher_rows(course: Dict[str, Any], course_id) -> List[Dict[str, Any]]:
    """Flatten both teacher lists of a fiche into `course_teachers` rows.

    `name_key` is computed with the backend's own helper so the read-time
    match against `people` uses exactly the same normalization.
    """
    rows: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for role, field in (("RESPONSABLE", "responsables"), ("TEACHING_TEAM", "equipe_pedagogique")):
        raw = course.get(field)
        if not isinstance(raw, list):
            continue
        for person in raw:
            if not isinstance(person, dict):
                continue
            name = _text(person.get("name"))
            if not name:
                continue
            key = person_name_key(name)
            if (role, key) in seen:
                continue
            seen.add((role, key))
            rows.append(
                {
                    "course_id": course_id,
                    "role": role,
                    "name": name,
                    "name_key": key,
                    "url": _text(person.get("url")),
                    "position": len(rows),
                }
            )
    return rows


def _strings(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(v).strip() for v in raw if v and str(v).strip()]


def _course_row(course: Dict[str, Any], source_id, now: datetime) -> Dict[str, Any]:
    return {
        "source_id": source_id,
        "external_id": str(course["id"]),
        "code": _first(course.get("code")),
        "title": _text(course.get("title")) or "Sans titre",
        "url": _first(course.get("url")),
        "schools": _strings(course.get("ecoles")),
        "niveau": _first(course.get("niveau")),
        "graduate": _first(course.get("graduate")),
        "domaine": _first(course.get("domaine")),
        "programme": _text(course.get("programme")),
        "langue_enseignement": _text(course.get("langue_enseignement")),
        "periode": _first(course.get("periode")),
        "lieu": _first(course.get("lieu")),
        "credits_ects": _number(course.get("credits_ects")),
        "heures_programmees": _number(course.get("heures_programmees")),
        "coefficient": _first(course.get("coefficient")),
        "departements": _strings(course.get("departement")),
        "organisation": _text(course.get("organisation")),
        "population": _text(course.get("population")),
        "mode_calcul_moyenne": _text(course.get("mode_calcul_moyenne")),
        "mode_calcul_credits": _text(course.get("mode_calcul_credits")),
        "introduction": _text(course.get("introduction")),
        "objectif": _text(course.get("objectif")),
        "contenu": _text(course.get("contenu")),
        "evaluations": _text(course.get("evaluations")),
        "plan_cours": _text(course.get("plan_cours")),
        "charge_travail_etudiant": _text(course.get("charge_travail_etudiant")),
        "description": _text(course.get("description")),
        "attributes": {
            k: v for k, v in (course.get("custom_fields") or {}).items() if v
        },
        "is_active": True,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
    }


# Everything except the identity (source_id, external_id) and created_at is
# refreshed on every run.
_UPDATED_COLUMNS = [
    c
    for c in Course.__table__.columns.keys()
    if c not in ("id", "source_id", "external_id", "created_at")
]


async def load_courses(db_session: AsyncSession, progress=None, task_id=None, log=print):
    if not JSON_PATH.exists():
        if progress and task_id is not None:
            progress.update(
                task_id,
                description="  [yellow]Load Courses: No JSON found. Skipping.[/yellow]",
                completed=1,
                total=1,
            )
        return

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)

    # The scraper writes {fetched_at, counts, courses, errors}; tolerate a bare
    # list in case the file was produced by hand.
    courses_data = payload.get("courses", []) if isinstance(payload, dict) else payload
    courses_data = [c for c in courses_data if c.get("id")]

    if progress and task_id is not None:
        progress.update(
            task_id,
            description=f"  [blue]Load Courses: Syncing {len(courses_data)} records...[/blue]",
            total=len(courses_data),
            completed=0,
        )

    log(f"Syncing [cyan]{len(courses_data)}[/cyan] courses to database...")

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    source_id = await get_data_source_id(db_session, SOURCE_CODE)
    now = _utc_now()

    try:
        existing_result = await db_session.execute(
            select(Course.external_id).where(Course.source_id == source_id)
        )
        known_ids = set(existing_result.scalars().all())

        created = updated = 0
        seen_ids = set()

        for course in courses_data:
            row = _course_row(course, source_id, now)
            if row["external_id"] in seen_ids:
                continue  # duplicate id inside the scrape — first one wins
            seen_ids.add(row["external_id"])

            stmt = insert(Course).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=["source_id", "external_id"],
                set_={col: stmt.excluded[col] for col in _UPDATED_COLUMNS},
            ).returning(Course.id)
            course_id = (await db_session.execute(stmt)).scalar_one()

            # Teachers are a full rewrite per course: the fiche is the sole
            # authority on who teaches, and nothing manual is attached to
            # these rows (the Person link is resolved at read time).
            await db_session.execute(
                delete(CourseTeacher).where(CourseTeacher.course_id == course_id)
            )
            teacher_rows = _teacher_rows(course, course_id)
            if teacher_rows:
                await db_session.execute(insert(CourseTeacher), teacher_rows)

            if row["external_id"] in known_ids:
                updated += 1
            else:
                created += 1

            if progress and task_id is not None:
                progress.update(task_id, advance=1)

        # Courses pulled from the catalog: keep the row, flag it inactive.
        stale_ids = known_ids - seen_ids
        deactivated = 0
        if stale_ids:
            await db_session.execute(
                update(Course)
                .where(
                    Course.source_id == source_id,
                    Course.external_id.in_(stale_ids),
                    Course.is_active.is_(True),
                )
                .values(is_active=False, updated_at=now)
            )
            deactivated = len(stale_ids)
            log(f"[yellow]{deactivated} courses no longer listed — deactivated.[/yellow]")

        await db_session.flush()
        await finish_ingestion_run(
            db_session,
            run,
            status="SUCCESS",
            created=created,
            updated=updated,
            deactivated=deactivated,
        )
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id is not None:
        progress.update(
            task_id,
            description=f"  [green]Load Courses: Done ({len(seen_ids)} courses).[/green]",
        )


async def main():
    async with AsyncSessionLocal() as session:
        await load_courses(session)
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
