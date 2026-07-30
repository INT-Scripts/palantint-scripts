import asyncio
import json
import os
import re
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import Organization, OrganizationMembership, Person
from palantint_scripts.utils import normalize_name

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import (
    close_stale_memberships,
    finish_ingestion_run,
    get_or_create_organization,
    start_ingestion_run,
    sync_membership,
)

DATA_DIR = str(SCRAPS_AUTO_DIR)

SOURCE_CODE = "groupes"

# Students missing from any Gp- group fall back to their base promo group.
PROMO_TO_BASE_GROUP = {
    ("Télécom SudParis", "Ingénieur 1ère année"): "Gp-EI1",
    ("Télécom SudParis", "Ingénieur 2ème année"): "Gp-EI2",
    ("Télécom SudParis", "Ingénieur 3ème année"): "Gp-EI3",
    ("IMT-BS", "Management 1ère année"): "Gp-EM1",
    ("IMT-BS", "Management 2ème année"): "Gp-EM2",
    ("IMT-BS", "Management 3ème année"): "Gp-EM3",
}

_GROUP_RE = re.compile(r"([Gg][Pp]-[A-Z]+[0-9])(-[A-Za-z0-9]+)?")


def parse_hierarchy_chain(gname: str) -> List[str]:
    """Expand a raw group name into its ancestor chain, root first, e.g.
    "Gp-EI1-G2a" -> ["Gp-EI1", "Gp-EI1-G", "Gp-EI1-G2", "Gp-EI1-G2a"].
    Each entry's parent in the returned list is the entry before it — this is
    a deliberate simplification of the old regex-only inference (which had no
    real notion of a tree at all) into an actual, if opinionated, hierarchy.
    Names that don't match the Gp-XXN pattern are treated as flat, single
    level groups.
    """
    m = _GROUP_RE.match(gname)
    if not m:
        return [gname]

    base = m.group(1)  # e.g. "Gp-EI1"
    suffix = m.group(2) or ""  # e.g. "-G2a"

    chain = [base]

    promo_name = f"{base}-G"
    if promo_name not in chain:
        chain.append(promo_name)

    if len(suffix) >= 3:  # e.g. "-G2a" -> also register "-G2"
        sub_parent_name = f"{base}{suffix[:len(suffix) - 1]}"
        if sub_parent_name not in chain:
            chain.append(sub_parent_name)

    if gname not in chain:
        chain.append(gname)

    return chain


async def _get_or_create_chain(db_session: AsyncSession, chain: List[str]) -> dict:
    """Idempotently upsert every level of a hierarchy chain as a real
    Organization(kind=CLASS_GROUP) row with parent_id links, returning
    {name: Organization}."""
    orgs = {}
    parent_id = None
    for name in chain:
        org = await get_or_create_organization(db_session, kind="CLASS_GROUP", name=name, parent_id=parent_id)
        orgs[name] = org
        parent_id = org.id
    return orgs


async def load_groupes(db_session: AsyncSession, progress=None, task_id=None, log=print):
    """
    Ingests group topologies into the database: builds the CLASS_GROUP
    Organization tree once (idempotent get-or-create per level) and syncs
    OrganizationMembership rows against it, instead of re-inferring the whole
    hierarchy from scratch on every run.
    """
    json_path = os.path.join(DATA_DIR, "groupes.json")
    if not os.path.exists(json_path):
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Groups: No JSON found. Skipping.[/yellow]", completed=1, total=1)
        return

    with open(json_path, "r", encoding="utf-8") as f:
        groupes_data = json.load(f)

    if not groupes_data:
        if progress and task_id:
            progress.update(task_id, description="  [yellow]Load Groups: JSON empty. Skipped.[/yellow]", completed=1, total=1)
        return

    run = await start_ingestion_run(db_session, SOURCE_CODE)
    updated_count = 0

    try:
        # 1. Map existing students for fuzzy matching
        log("Mapping students for identity matching...")
        result = await db_session.execute(select(Person).where(Person.kind == "STUDENT"))
        all_students = result.scalars().all()

        student_map = {}
        for s in all_students:
            n1 = normalize_name(f"{s.first_name}{s.last_name}")
            n2 = normalize_name(f"{s.last_name}{s.first_name}")
            if n1: student_map[n1] = s
            if n2: student_map[n2] = s

        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Load Groups: Syncing {len(groupes_data)} groups...[/blue]", total=len(groupes_data), completed=0)

        # 2. Build the Organization tree + collect per-student target group chains
        log("Building class-group hierarchy and collecting memberships...")
        # person_id -> set of Organization ids they should be an active member of
        target_memberships: dict = {}
        students_with_groups = set()

        for group_info in groupes_data:
            gname = group_info["name"]
            member_ids = {
                student_map[normalize_name(m["name"])].id
                for m in group_info.get("members", [])
                if normalize_name(m["name"]) in student_map
            }

            chain = parse_hierarchy_chain(gname)
            chain_orgs = await _get_or_create_chain(db_session, chain)

            for sid in member_ids:
                target_memberships.setdefault(sid, set()).update(o.id for o in chain_orgs.values())
                if chain[0].lower().startswith("gp"):
                    students_with_groups.add(sid)

            if progress and task_id: progress.update(task_id, advance=1)

        # 3. Promo-based fallback for students missing from any Gp- group
        log("Applying promo-based global group fallbacks...")
        for s in all_students:
            if s.id in students_with_groups:
                continue

            target_gp = None

            # Resolve the student's active PROMO membership, then its parent SCHOOL.
            promo_result = await db_session.execute(
                select(Organization)
                .select_from(OrganizationMembership)
                .join(Organization, Organization.id == OrganizationMembership.organization_id)
                .where(
                    OrganizationMembership.person_id == s.id,
                    OrganizationMembership.ended_at.is_(None),
                    Organization.kind == "PROMO",
                )
            )
            promo_org = promo_result.scalars().first()

            if promo_org:
                promo_name = promo_org.name
                school_name = None
                if promo_org.parent_id:
                    school_org = await db_session.get(Organization, promo_org.parent_id)
                    school_name = school_org.name if school_org else None

                target_gp = PROMO_TO_BASE_GROUP.get((school_name, promo_name))
                if not target_gp:
                    if school_name == "Télécom SudParis":
                        if "1ère" in str(promo_name): target_gp = "Gp-EI1"
                        elif "2ème" in str(promo_name): target_gp = "Gp-EI2"
                        elif "3ème" in str(promo_name): target_gp = "Gp-EI3"
                    elif school_name == "IMT-BS":
                        if "1ère" in str(promo_name): target_gp = "Gp-EM1"
                        elif "2ème" in str(promo_name): target_gp = "Gp-EM2"
                        elif "3ème" in str(promo_name): target_gp = "Gp-EM3"

            if target_gp:
                chain = parse_hierarchy_chain(target_gp)
                chain_orgs = await _get_or_create_chain(db_session, chain)
                target_memberships.setdefault(s.id, set()).update(o.id for o in chain_orgs.values())

        # 4. Sync memberships: activate targets, close out anything no longer current.
        log("Syncing class-group memberships...")
        deactivated_count = 0
        for person_id, org_ids in target_memberships.items():
            for org_id in org_ids:
                await sync_membership(db_session, person_id, org_id, source_code=SOURCE_CODE)
                updated_count += 1
            deactivated_count += await close_stale_memberships(db_session, person_id, "CLASS_GROUP", org_ids)

        await db_session.flush()
        await finish_ingestion_run(db_session, run, status="SUCCESS", updated=updated_count, deactivated=deactivated_count)
        log(f"[green]Synced class-group memberships for {len(target_memberships)} students.[/green]")

    except Exception as e:
        await finish_ingestion_run(db_session, run, status="FAILED", error=str(e)[:2000])
        raise

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Load Groups: Done ({len(groupes_data)} groups).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        await load_groupes(session)
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
