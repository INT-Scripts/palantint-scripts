import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import ExternalIdentity, Organization, OrganizationMembership, Person
from palantint_scripts.utils import normalize_name

from palantint_scripts.config import SCRAPS_AUTO_DIR
from palantint_scripts.db_helpers import (
    close_stale_memberships,
    finish_ingestion_run,
    get_data_source_id,
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

_GROUP_RE = re.compile(r"(?i)^(gp-[a-z]+[0-9])(?:-(.+))?$")


def parse_hierarchy_chain(gname: str) -> List[str]:
    """Expand a raw group name into its ancestor chain, root first, e.g.
    "Gp-EI1-G2a" -> ["Gp-EI1", "Gp-EI1-G", "Gp-EI1-G2", "Gp-EI1-G2a"].
    Course-specific suffixes retain their full prefix, so a membership in
    Gp-EI2-SIC4103-G1 does not imply membership in the unrelated Gp-EI2-G.
    Names outside the Gp-XXN pattern stay flat.
    """
    m = _GROUP_RE.match(gname)
    if not m:
        return [gname]

    base = m.group(1)
    suffix = m.group(2)
    chain = [base]
    if not suffix:
        return chain
    segments = suffix.split("-")
    if len(segments) == 1 and segments[0].lower().startswith("g"):
        chain.append(f"{base}-G")
        if re.fullmatch(r"(?i)G\d+[a-z]", segments[0]):
            chain.append(f"{base}-{segments[0][:-1]}")
    for index in range(1, len(segments) + 1):
        candidate = f"{base}-{'-'.join(segments[:index])}"
        if candidate not in chain:
            chain.append(candidate)
    return chain


def _roster_name_parts(name: str) -> tuple[str, str]:
    """SI Annuaire lists family names in uppercase before given names."""
    parts = name.split()
    boundary = next((i for i, part in enumerate(parts) if part != part.upper()), len(parts))
    if boundary == len(parts):
        boundary = max(1, len(parts) - 1)
    return " ".join(parts[boundary:]), " ".join(parts[:boundary])


async def _get_or_create_chain(db_session: AsyncSession, chain: List[str]) -> dict:
    """Idempotently upsert every level of a hierarchy chain as a real
    Organization(kind=CLASS_GROUP) row with parent_id links, returning
    {name: Organization}."""
    orgs = {}
    parent_id = None
    for name in chain:
        org = await get_or_create_organization(db_session, kind="CLASS_GROUP", name=name, parent_id=parent_id)
        if org.kind == "CLASS_GROUP" and org.parent_id != parent_id:
            org.parent_id = parent_id
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

    complete_scope = False
    scope_path = Path(DATA_DIR) / "groupes_scope.json"
    if scope_path.exists():
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        if scope.get("status") == "running":
            raise RuntimeError("Group roster snapshot is still being scraped")
        if scope.get("status") == "complete":
            target_ids = set(scope.get("target_ids", []))
            available_ids = {str(group.get("id")) for group in groupes_data}
            missing_ids = target_ids - available_ids
            if missing_ids:
                raise RuntimeError(f"Missing {len(missing_ids)} groups from completed roster snapshot")
            groupes_data = [group for group in groupes_data if str(group.get("id")) in target_ids]
            complete_scope = True

    if not groupes_data and not complete_scope:
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

        student_map: dict[str, set] = {}
        for s in all_students:
            n1 = normalize_name(f"{s.first_name}{s.last_name}")
            n2 = normalize_name(f"{s.last_name}{s.first_name}")
            if n1: student_map.setdefault(n1, set()).add(s.id)
            if n2: student_map.setdefault(n2, set()).add(s.id)

        source_id = await get_data_source_id(db_session, SOURCE_CODE)
        identity_rows = await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.source_id == source_id))
        identity_by_external_id = {identity.external_id: identity for identity in identity_rows.scalars()}
        resolved_roster_ids: dict[str, uuid.UUID] = {}
        for group_info in groupes_data:
            for member in group_info.get("members", []):
                external_id = str(member.get("id_objet") or "")
                name = member.get("name") or ""
                normalized = normalize_name(name)
                if not normalized:
                    continue
                if external_id in resolved_roster_ids:
                    continue
                identity = identity_by_external_id.get(external_id) if external_id else None
                if identity:
                    person_id = identity.person_id
                else:
                    candidates = student_map.get(normalized, set())
                    if len(candidates) > 1:
                        log(f"[yellow]Ambiguous roster identity {external_id}; skipping.[/yellow]")
                        continue
                    if candidates:
                        person_id = next(iter(candidates))
                    else:
                        if not external_id:
                            continue
                        first_name, last_name = _roster_name_parts(name)
                        person = Person(kind="STUDENT", first_name=first_name, last_name=last_name)
                        db_session.add(person)
                        await db_session.flush()
                        person_id = person.id
                        all_students.append(person)
                        student_map.setdefault(normalized, set()).add(person_id)
                    if external_id:
                        identity = ExternalIdentity(person_id=person_id, source_id=source_id, external_id=external_id)
                        db_session.add(identity)
                        identity_by_external_id[external_id] = identity
                if external_id:
                    resolved_roster_ids[external_id] = person_id

        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Load Groups: Syncing {len(groupes_data)} groups...[/blue]", total=len(groupes_data), completed=0)

        # 2. Build the Organization tree + collect per-student target group chains
        log("Building class-group hierarchy and collecting memberships...")
        # person_id -> set of Organization ids they should be an active member of
        target_memberships: dict = {}
        students_with_groups = set()

        for group_info in groupes_data:
            gname = group_info["name"]
            member_ids = set()
            for member in group_info.get("members", []):
                external_id = str(member.get("id_objet") or "")
                normalized = normalize_name(member.get("name") or "")
                if external_id in resolved_roster_ids:
                    member_ids.add(resolved_roster_ids[external_id])
                elif len(student_map.get(normalized, set())) == 1:
                    member_ids.update(student_map[normalized])

            chain = parse_hierarchy_chain(gname)
            chain_orgs = await _get_or_create_chain(db_session, chain)

            for sid in member_ids:
                target_memberships.setdefault(sid, set()).update(o.id for o in chain_orgs.values())
                if chain[0].lower().startswith("gp"):
                    students_with_groups.add(sid)

            if progress and task_id: progress.update(task_id, advance=1)

        # 3. Promo-based fallback for students missing from any Gp- group
        if not complete_scope:
            log("Applying promo-based global group fallbacks...")
        for s in ([] if complete_scope else all_students):
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
                if len(chain) == 1 and target_gp.lower().startswith("gp-"):
                    chain.append(f"{target_gp}-G")
                chain_orgs = await _get_or_create_chain(db_session, chain)
                target_memberships.setdefault(s.id, set()).update(o.id for o in chain_orgs.values())

        # 4. Sync memberships: activate targets, close out anything no longer current.
        log("Syncing class-group memberships...")
        if complete_scope:
            for student in all_students:
                target_memberships.setdefault(student.id, set())
        deactivated_count = 0
        for person_id, org_ids in target_memberships.items():
            for org_id in org_ids:
                await sync_membership(db_session, person_id, org_id, source_code=SOURCE_CODE)
                updated_count += 1
            deactivated_count += await close_stale_memberships(db_session, person_id, "CLASS_GROUP", org_ids, source_code=SOURCE_CODE)

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
