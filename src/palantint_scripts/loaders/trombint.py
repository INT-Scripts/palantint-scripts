import asyncio
import json
import os
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from db.database import AsyncSessionLocal
from db.models import ExternalIdentity, Person

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import (
    close_stale_memberships,
    finish_ingestion_run,
    get_data_source_id,
    get_or_create_organization,
    start_ingestion_run,
    sync_membership,
    upsert_person_by_external_id,
    utc_now,
)

DATA_DIR = str(SCRAPS_AUTO_DIR)

SOURCE_CODE = "trombint"

def _format_name(nom_complet: str):
    parts = nom_complet.split()
    first_parts, last_parts = [], []
    for p in parts:
        if p == p.upper() and len(p) > 1: last_parts.append(p.capitalize())
        elif last_parts: last_parts.append(p.capitalize())
        else: first_parts.append(p)
    return (" ".join(first_parts) or (parts[0] if parts else "")), (" ".join(last_parts) or "")

async def load_trombint(db_session: AsyncSession, progress=None, task_id=None, log=print):
    json_path = os.path.join(DATA_DIR, "students.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Update Students: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        all_students_data = json.load(f)

    if not all_students_data:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Update Students: JSON empty. Skipped.[/yellow]", completed=1, total=1)
        return

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    created_count = updated_count = deactivated_count = 0

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Update Students: Syncing {len(all_students_data)} records...[/blue]", total=len(all_students_data), completed=0)

    found_uids = set()
    now = utc_now()

    try:
        for s in all_students_data:
            uid = s.get("uid") or s.get("trombint_id")
            if not uid: continue
            found_uids.add(uid)

            first_name, last_name = _format_name(s.get("nom_complet", f"{s.get('first_name', '')} {s.get('last_name', '')}"))

            person, created = await upsert_person_by_external_id(
                db_session,
                source_code=SOURCE_CODE,
                external_id=uid,
                kind="STUDENT",
                fields={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": s.get("email", ""),
                    "profile_picture_path": s.get("photo_url", s.get("profile_picture_path", "")),
                    "is_active": True,
                    "last_seen_at": now,
                },
            )
            created_count += int(created)
            updated_count += int(not created)

            # ecole/promo -> OrganizationMembership against the seeded PROMO org.
            # Close out any previous PROMO membership (e.g. last year's promo after
            # a year-group promotion) so a person never accumulates more than one
            # concurrently-active PROMO membership.
            promo_name = s.get("promo")
            if promo_name:
                promo_org = await get_or_create_organization(db_session, kind="PROMO", name=promo_name)
                await sync_membership(db_session, person.id, promo_org.id, source_code=SOURCE_CODE)
                await close_stale_memberships(db_session, person.id, "PROMO", {promo_org.id})

            if progress and task_id: progress.update(task_id, advance=1)

        # Safety: only mark inactive if we have a high-confidence batch
        if len(found_uids) > 100:
            source_id = await get_data_source_id(db_session, SOURCE_CODE)
            result = await db_session.execute(
                select(Person.id)
                .join(ExternalIdentity, ExternalIdentity.person_id == Person.id)
                .where(
                    ExternalIdentity.source_id == source_id,
                    ExternalIdentity.external_id.not_in(list(found_uids)),
                    Person.is_active.is_(True),
                )
            )
            stale_ids = [row[0] for row in result.all()]
            if stale_ids:
                await db_session.execute(
                    update(Person).where(Person.id.in_(stale_ids)).values(is_active=False)
                )
                deactivated_count = len(stale_ids)

        await db_session.flush()
        await finish_ingestion_run(
            db_session, run, status="SUCCESS",
            created=created_count, updated=updated_count, deactivated=deactivated_count,
        )
    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Update Students: Done.[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_trombint(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
