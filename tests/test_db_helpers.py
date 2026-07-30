import uuid

import pytest
from sqlalchemy import select

from db.models import IngestionRun, OrganizationMembership, Person
from palantint_scripts.db_helpers import (
    close_stale_memberships,
    finish_ingestion_run,
    get_data_source_id,
    get_or_create_organization,
    start_ingestion_run,
    sync_membership,
    upsert_person_by_external_id,
)


@pytest.mark.asyncio
async def test_get_data_source_id_known_and_unknown(db_session):
    source_id = await get_data_source_id(db_session, "trombint")
    assert isinstance(source_id, uuid.UUID)

    # Cached: second call returns the same id without re-querying.
    assert await get_data_source_id(db_session, "trombint") == source_id

    with pytest.raises(ValueError):
        await get_data_source_id(db_session, "not_a_real_source")


@pytest.mark.asyncio
async def test_ingestion_run_lifecycle(db_session):
    run = await start_ingestion_run(db_session, "trombint")
    assert run.status == "RUNNING"
    assert run.finished_at is None

    await finish_ingestion_run(db_session, run, status="SUCCESS", created=3, updated=2, deactivated=1)
    assert run.status == "SUCCESS"
    assert run.finished_at is not None
    assert run.records_created == 3
    assert run.records_updated == 2
    assert run.records_deactivated == 1

    result = await db_session.execute(select(IngestionRun).where(IngestionRun.id == run.id))
    persisted = result.scalars().first()
    assert persisted is not None
    assert persisted.status == "SUCCESS"


@pytest.mark.asyncio
async def test_ingestion_run_failure_records_error(db_session):
    run = await start_ingestion_run(db_session, "trombint")
    await finish_ingestion_run(db_session, run, status="FAILED", error="boom")
    assert run.status == "FAILED"
    assert run.error == "boom"


@pytest.mark.asyncio
async def test_get_or_create_organization_is_idempotent_by_name(db_session):
    org1 = await get_or_create_organization(db_session, kind="CLUB", name="Robotics Club")
    org2 = await get_or_create_organization(db_session, kind="CLUB", name="Robotics Club")
    assert org1.id == org2.id

    # kind/parent_id are only applied on first creation -- a second call with
    # a different kind must NOT change the existing row's kind.
    org3 = await get_or_create_organization(db_session, kind="BUREAU", name="Robotics Club")
    assert org3.id == org1.id
    assert org3.kind == "CLUB"


@pytest.mark.asyncio
async def test_get_or_create_organization_builds_parent_chain(db_session):
    parent = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Gp-EI1")
    child = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Gp-EI1-G", parent_id=parent.id)
    assert child.parent_id == parent.id


@pytest.mark.asyncio
async def test_upsert_person_by_external_id_creates_then_updates(db_session):
    person1, created1 = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe",
        fields={"first_name": "John", "last_name": "Doe", "email": "john@example.com"},
    )
    assert created1 is True
    assert person1.first_name == "John"

    person2, created2 = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe",
        fields={"first_name": "Johnny", "last_name": "Doe", "email": "john@example.com"},
    )
    assert created2 is False
    assert person2.id == person1.id
    assert person2.first_name == "Johnny"

    # Exactly one Person row exists for this identity.
    result = await db_session.execute(select(Person))
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_upsert_person_by_external_id_none_fields_dont_clobber(db_session):
    person1, _ = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe",
        fields={"first_name": "John", "last_name": "Doe", "email": "john@example.com"},
    )
    person2, _ = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe",
        fields={"first_name": "John", "last_name": "Doe", "email": None},
    )
    assert person2.id == person1.id
    # A None field value must not overwrite the previously-set email.
    assert person2.email == "john@example.com"


@pytest.mark.asyncio
async def test_upsert_person_by_external_id_different_sources_are_different_identities(db_session):
    person_a, created_a = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="same-id",
        fields={"first_name": "A", "last_name": "A"},
    )
    person_b, created_b = await upsert_person_by_external_id(
        db_session, source_code="groupes", external_id="same-id",
        fields={"first_name": "B", "last_name": "B"},
    )
    assert created_a and created_b
    assert person_a.id != person_b.id


@pytest.mark.asyncio
async def test_sync_membership_create_and_reactivate(db_session):
    person, _ = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe", fields={"first_name": "J", "last_name": "D"},
    )
    org = await get_or_create_organization(db_session, kind="CLUB", name="Chess Club")

    membership = await sync_membership(db_session, person.id, org.id, source_code="clubs", role="Membre")
    assert membership.ended_at is None
    assert membership.role == "Membre"

    membership.ended_at = membership.started_at  # simulate a closed-out membership

    reactivated = await sync_membership(db_session, person.id, org.id, source_code="clubs", role="President", is_mandat=True)
    assert reactivated.id == membership.id
    assert reactivated.ended_at is None
    assert reactivated.role == "President"
    assert reactivated.is_mandat is True

    # Still exactly one membership row for this (person, org) pair.
    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == person.id,
            OrganizationMembership.organization_id == org.id,
        )
    )
    assert len(result.scalars().all()) == 1


@pytest.mark.asyncio
async def test_close_stale_memberships_closes_non_kept_only(db_session):
    person, _ = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe", fields={"first_name": "J", "last_name": "D"},
    )
    org_a = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Gp-EI1")
    org_b = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Gp-EI2")
    other_kind_org = await get_or_create_organization(db_session, kind="CLUB", name="Chess Club")

    await sync_membership(db_session, person.id, org_a.id, source_code="groupes")
    await sync_membership(db_session, person.id, org_b.id, source_code="groupes")
    await sync_membership(db_session, person.id, other_kind_org.id, source_code="clubs")

    closed = await close_stale_memberships(db_session, person.id, "CLASS_GROUP", {org_b.id})
    assert closed == 1

    result = await db_session.execute(
        select(OrganizationMembership).where(OrganizationMembership.person_id == person.id)
    )
    memberships = {m.organization_id: m for m in result.scalars().all()}
    assert memberships[org_a.id].ended_at is not None
    assert memberships[org_b.id].ended_at is None
    # A different organization kind must never be touched.
    assert memberships[other_kind_org.id].ended_at is None


@pytest.mark.asyncio
async def test_close_stale_memberships_empty_keep_set_closes_all_of_kind(db_session):
    person, _ = await upsert_person_by_external_id(
        db_session, source_code="trombint", external_id="jdoe", fields={"first_name": "J", "last_name": "D"},
    )
    org_a = await get_or_create_organization(db_session, kind="PROMO", name="Promo A")
    org_b = await get_or_create_organization(db_session, kind="PROMO", name="Promo B")
    await sync_membership(db_session, person.id, org_a.id, source_code="trombint")
    await sync_membership(db_session, person.id, org_b.id, source_code="trombint")

    closed = await close_stale_memberships(db_session, person.id, "PROMO", set())
    assert closed == 2

    result = await db_session.execute(
        select(OrganizationMembership).where(OrganizationMembership.person_id == person.id)
    )
    assert all(m.ended_at is not None for m in result.scalars().all())
