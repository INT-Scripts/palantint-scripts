import pytest
from sqlalchemy import select

from db.models import ExternalIdentity, Organization, OrganizationMembership, Person
from palantint_scripts.loaders import trombint as trombint_loader

from tests.conftest import write_json


@pytest.mark.asyncio
async def test_load_trombint_no_json_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    await trombint_loader.load_trombint(db_session)  # students.json doesn't exist
    result = await db_session.execute(select(Person))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_trombint_empty_json_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "students.json", [])
    await trombint_loader.load_trombint(db_session)
    result = await db_session.execute(select(Person))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_trombint_creates_person_and_identity_and_promo(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "students.json", [
        {"uid": "jdoe", "nom_complet": "John DOE", "email": "john@example.com", "ecole": "Télécom SudParis", "promo": "Ingénieur 1ère année"},
    ])
    await db_session.flush()
    await trombint_loader.load_trombint(db_session)

    result = await db_session.execute(select(Person))
    people = result.scalars().all()
    assert len(people) == 1
    person = people[0]
    assert person.first_name == "John"
    assert person.last_name == "Doe"
    assert person.kind == "STUDENT"
    assert person.is_active is True

    result = await db_session.execute(select(ExternalIdentity).where(ExternalIdentity.person_id == person.id))
    identity = result.scalars().first()
    assert identity.external_id == "jdoe"

    result = await db_session.execute(
        select(Organization)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(OrganizationMembership.person_id == person.id, Organization.kind == "PROMO")
    )
    promo = result.scalars().first()
    assert promo.name == "Ingénieur 1ère année"


@pytest.mark.asyncio
async def test_load_trombint_missing_uid_is_skipped(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "students.json", [
        {"nom_complet": "NO ID Person", "email": "noid@example.com"},
    ])
    await trombint_loader.load_trombint(db_session)
    result = await db_session.execute(select(Person))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_trombint_promo_switch_closes_old_membership(db_session, tmp_path, monkeypatch):
    """Regression test for the promo-membership-leak bug: a student promoted
    from year 1 to year 2 must end up with exactly one active PROMO
    membership, with the old one closed out (ended_at set), not both active."""
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))

    write_json(tmp_path, "students.json", [
        {"uid": "jdoe", "nom_complet": "John DOE", "ecole": "Télécom SudParis", "promo": "Ingénieur 1ère année"},
    ])
    await trombint_loader.load_trombint(db_session)

    write_json(tmp_path, "students.json", [
        {"uid": "jdoe", "nom_complet": "John DOE", "ecole": "Télécom SudParis", "promo": "Ingénieur 2ème année"},
    ])
    await trombint_loader.load_trombint(db_session)

    result = await db_session.execute(select(Person))
    person = result.scalars().one()

    result = await db_session.execute(
        select(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(OrganizationMembership.person_id == person.id, Organization.kind == "PROMO")
    )
    memberships = result.scalars().all()
    assert len(memberships) == 2, "expected both the old and new promo membership rows to still exist (history preserved)"

    active = [m for m in memberships if m.ended_at is None]
    closed = [m for m in memberships if m.ended_at is not None]
    assert len(active) == 1, "student must have exactly one active PROMO membership after a promo switch"
    assert len(closed) == 1

    active_org = await db_session.get(Organization, active[0].organization_id)
    closed_org = await db_session.get(Organization, closed[0].organization_id)
    assert active_org.name == "Ingénieur 2ème année"
    assert closed_org.name == "Ingénieur 1ère année"


@pytest.mark.asyncio
async def test_load_trombint_same_promo_resync_stays_single_active(db_session, tmp_path, monkeypatch):
    """Re-syncing the same student with the SAME promo twice must not create
    a second membership row or spuriously close/reopen the existing one."""
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    data = [{"uid": "jdoe", "nom_complet": "John DOE", "ecole": "Télécom SudParis", "promo": "Ingénieur 1ère année"}]
    write_json(tmp_path, "students.json", data)
    await trombint_loader.load_trombint(db_session)
    await trombint_loader.load_trombint(db_session)

    result = await db_session.execute(select(Person))
    person = result.scalars().one()
    result = await db_session.execute(
        select(OrganizationMembership).where(OrganizationMembership.person_id == person.id)
    )
    memberships = result.scalars().all()
    assert len(memberships) == 1
    assert memberships[0].ended_at is None


@pytest.mark.asyncio
async def test_load_trombint_deactivation_threshold_boundary(db_session, tmp_path, monkeypatch):
    """The safety net only deactivates missing students when the batch has
    > 100 records (a low-confidence/truncated scrape shouldn't wipe out the
    whole directory's is_active flag). Verify both sides of that boundary."""
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))

    # First sync: 101 students (> 100), all active.
    big_batch = [{"uid": f"s{i}", "nom_complet": f"Student {i}"} for i in range(101)]
    write_json(tmp_path, "students.json", big_batch)
    await trombint_loader.load_trombint(db_session)

    result = await db_session.execute(select(Person))
    assert len(result.scalars().all()) == 101
    assert all(p.is_active for p in result.scalars().all())

    # Second sync: only 5 students found (a truncated/low-confidence scrape).
    # Because 5 <= 100, nobody should be deactivated even though 96 are missing.
    small_batch = [{"uid": f"s{i}", "nom_complet": f"Student {i}"} for i in range(5)]
    write_json(tmp_path, "students.json", small_batch)
    await trombint_loader.load_trombint(db_session)

    result = await db_session.execute(select(Person))
    people = result.scalars().all()
    assert len(people) == 101, "no students should be deleted, only possibly deactivated"
    assert all(p.is_active for p in people), "a <=100-record batch must never deactivate anyone"
