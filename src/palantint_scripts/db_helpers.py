"""Shared DB helpers for ETL loaders working against the Person/Organization/
Location provenance-aware schema. `db.seed.seed_default_data` (run as the
"Setup Infrastructure" step before any loader in sync.py) guarantees the
DataSource registry and base SCHOOL/PROMO Organization rows already exist by
the time loaders run.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    DataSource,
    ExternalIdentity,
    IngestionRun,
    Organization,
    OrganizationMembership,
    Person,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Data sources & ingestion runs ────────────────────────────────────────────

_data_source_cache: Dict[str, uuid.UUID] = {}


async def get_data_source_id(db_session: AsyncSession, code: str) -> uuid.UUID:
    if code in _data_source_cache:
        return _data_source_cache[code]
    result = await db_session.execute(select(DataSource.id).where(DataSource.code == code))
    source_id = result.scalar_one_or_none()
    if source_id is None:
        raise ValueError(f"Unknown DataSource code '{code}' — is db.seed.seed_default_data missing it?")
    _data_source_cache[code] = source_id
    return source_id


async def start_ingestion_run(db_session: AsyncSession, source_code: str) -> IngestionRun:
    source_id = await get_data_source_id(db_session, source_code)
    run = IngestionRun(source_id=source_id, status="RUNNING")
    db_session.add(run)
    await db_session.flush()
    return run


async def finish_ingestion_run(
    db_session: AsyncSession,
    run: IngestionRun,
    status: str = "SUCCESS",
    created: int = 0,
    updated: int = 0,
    deactivated: int = 0,
    error: Optional[str] = None,
):
    run.status = status
    run.finished_at = utc_now()
    run.records_created = created
    run.records_updated = updated
    run.records_deactivated = deactivated
    run.error = error
    db_session.add(run)
    await db_session.flush()


# ── Organizations ─────────────────────────────────────────────────────────────

async def get_or_create_organization(
    db_session: AsyncSession,
    kind: str,
    name: str,
    parent_id: Optional[uuid.UUID] = None,
    **extra_fields: Any,
) -> Organization:
    """Organization.name is globally unique, so lookup/creation is by name
    alone; `kind`/`parent_id` are only applied on first creation."""
    result = await db_session.execute(select(Organization).where(Organization.name == name))
    org = result.scalars().first()
    if org:
        return org

    org = Organization(kind=kind, name=name, parent_id=parent_id, **extra_fields)
    db_session.add(org)
    await db_session.flush()
    return org


# ── People ────────────────────────────────────────────────────────────────────

async def upsert_person_by_external_id(
    db_session: AsyncSession,
    source_code: str,
    external_id: str,
    fields: Dict[str, Any],
    kind: str = "STUDENT",
) -> tuple[Person, bool]:
    """Find a Person via its ExternalIdentity(source_code, external_id), or
    create both if this is the first time this identity has been seen.
    Returns (person, created)."""
    source_id = await get_data_source_id(db_session, source_code)

    result = await db_session.execute(
        select(ExternalIdentity).where(
            ExternalIdentity.source_id == source_id,
            ExternalIdentity.external_id == external_id,
        )
    )
    identity = result.scalars().first()

    now = utc_now()

    if identity:
        person = await db_session.get(Person, identity.person_id)
        for k, v in fields.items():
            if v is not None:
                setattr(person, k, v)
        identity.last_seen_at = now
        return person, False

    person = Person(kind=kind, **{k: v for k, v in fields.items() if v is not None})
    db_session.add(person)
    await db_session.flush()

    identity = ExternalIdentity(
        person_id=person.id,
        source_id=source_id,
        external_id=external_id,
        first_seen_at=now,
        last_seen_at=now,
    )
    db_session.add(identity)
    await db_session.flush()
    return person, True


# ── Organization memberships ─────────────────────────────────────────────────

async def sync_membership(
    db_session: AsyncSession,
    person_id: uuid.UUID,
    organization_id: uuid.UUID,
    source_code: Optional[str] = None,
    role: str = "Membre",
    is_mandat: bool = False,
) -> OrganizationMembership:
    """Ensure an active membership row exists for (person, organization).
    Reactivates a closed-out row rather than inserting a duplicate."""
    source_id = await get_data_source_id(db_session, source_code) if source_code else None

    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == person_id,
            OrganizationMembership.organization_id == organization_id,
        )
    )
    membership = result.scalars().first()

    if membership:
        if membership.ended_at is not None:
            membership.ended_at = None
            membership.started_at = utc_now()
        membership.role = role
        membership.is_mandat = is_mandat
        if source_id:
            membership.source_id = source_id
        return membership

    membership = OrganizationMembership(
        person_id=person_id,
        organization_id=organization_id,
        role=role,
        is_mandat=is_mandat,
        source_id=source_id,
    )
    db_session.add(membership)
    await db_session.flush()
    return membership


async def close_stale_memberships(
    db_session: AsyncSession,
    person_id: uuid.UUID,
    organization_kind: str,
    keep_organization_ids: set,
) -> int:
    """Close out (set ended_at) active memberships of the given kind for this
    person that aren't in `keep_organization_ids`. Preserves history instead
    of delete+reinsert. Returns the number of rows closed."""
    result = await db_session.execute(
        select(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.person_id == person_id,
            OrganizationMembership.ended_at.is_(None),
            Organization.kind == organization_kind,
            OrganizationMembership.organization_id.not_in(keep_organization_ids) if keep_organization_ids else True,
        )
    )
    stale = result.scalars().all()
    now = utc_now()
    for m in stale:
        m.ended_at = now
    return len(stale)
