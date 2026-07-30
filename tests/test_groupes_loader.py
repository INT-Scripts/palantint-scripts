import pytest
from sqlalchemy import select

from db.models import Organization, OrganizationMembership, Person
from palantint_scripts.loaders import groupes as groupes_loader
from palantint_scripts.loaders import trombint as trombint_loader

from tests.conftest import write_json


def test_parse_hierarchy_chain_expands_all_levels():
    chain = groupes_loader.parse_hierarchy_chain("Gp-EI1-G2a")
    assert chain == ["Gp-EI1", "Gp-EI1-G", "Gp-EI1-G2", "Gp-EI1-G2a"]


def test_parse_hierarchy_chain_short_suffix_skips_level2():
    # "-Ga" suffix (len < 3) shouldn't produce an intermediate "Gp-EI1-G" + "a" split.
    chain = groupes_loader.parse_hierarchy_chain("Gp-EI1-Ga")
    assert chain[0] == "Gp-EI1"
    assert chain[-1] == "Gp-EI1-Ga"


def test_parse_hierarchy_chain_non_matching_name_is_flat():
    chain = groupes_loader.parse_hierarchy_chain("Some Random Club")
    assert chain == ["Some Random Club"]


async def _seed_student(db_session, tmp_path, uid, name, ecole, promo):
    write_json(tmp_path, "students.json", [{"uid": uid, "nom_complet": name, "ecole": ecole, "promo": promo}])
    await trombint_loader.load_trombint(db_session)


@pytest.mark.asyncio
async def test_load_groupes_no_json_is_a_noop(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    await groupes_loader.load_groupes(db_session)
    result = await db_session.execute(select(OrganizationMembership))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_groupes_builds_hierarchy_and_memberships(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    await _seed_student(db_session, tmp_path, "jdoe", "John DOE", "Télécom SudParis", "Ingénieur 1ère année")

    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "groupes.json", [
        {"name": "Gp-EI1-G2a", "members": [{"name": "John DOE"}]},
    ])
    await groupes_loader.load_groupes(db_session)

    result = await db_session.execute(select(Person))
    person = result.scalars().one()

    result = await db_session.execute(
        select(Organization.name)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(OrganizationMembership.person_id == person.id, OrganizationMembership.ended_at.is_(None))
    )
    names = {row[0] for row in result.all()}
    # Member of the explicit group AND every ancestor level in the chain.
    assert {"Gp-EI1", "Gp-EI1-G", "Gp-EI1-G2", "Gp-EI1-G2a"} <= names

    # The chain is a real tree, not flat rows.
    result = await db_session.execute(select(Organization).where(Organization.name == "Gp-EI1-G2a"))
    leaf = result.scalars().one()
    result = await db_session.execute(select(Organization).where(Organization.name == "Gp-EI1-G2"))
    level2 = result.scalars().one()
    assert leaf.parent_id == level2.id


@pytest.mark.asyncio
async def test_load_groupes_promo_fallback_for_ungrouped_students(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    await _seed_student(db_session, tmp_path, "jdoe", "John DOE", "Télécom SudParis", "Ingénieur 2ème année")

    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    # groupes.json has content but this student appears in none of it --
    # promo-based fallback should still land them in Gp-EI2 / Gp-EI2-G.
    write_json(tmp_path, "groupes.json", [
        {"name": "Gp-EM1", "members": [{"name": "Someone Else"}]},
    ])
    await groupes_loader.load_groupes(db_session)

    result = await db_session.execute(select(Person))
    person = result.scalars().one()
    result = await db_session.execute(
        select(Organization.name)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(OrganizationMembership.person_id == person.id, OrganizationMembership.ended_at.is_(None))
    )
    names = {row[0] for row in result.all()}
    assert "Gp-EI2" in names
    assert "Gp-EI2-G" in names


@pytest.mark.asyncio
async def test_load_groupes_resync_closes_out_dropped_membership(db_session, tmp_path, monkeypatch):
    """A student removed from a class group in a later sync must have that
    membership closed (ended_at set), not left dangling as active forever."""
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(tmp_path))
    await _seed_student(db_session, tmp_path, "jdoe", "John DOE", "Télécom SudParis", "Ingénieur 1ère année")

    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "groupes.json", [{"name": "Gp-EI1-G2a", "members": [{"name": "John DOE"}]}])
    await groupes_loader.load_groupes(db_session)

    # Student moves to a different sub-group entirely.
    write_json(tmp_path, "groupes.json", [{"name": "Gp-EI1-G3b", "members": [{"name": "John DOE"}]}])
    await groupes_loader.load_groupes(db_session)

    result = await db_session.execute(select(Person))
    person = result.scalars().one()

    result = await db_session.execute(select(Organization).where(Organization.name == "Gp-EI1-G2a"))
    old_leaf = result.scalars().one()
    result = await db_session.execute(select(Organization).where(Organization.name == "Gp-EI1-G3b"))
    new_leaf = result.scalars().one()

    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == person.id,
            OrganizationMembership.organization_id == old_leaf.id,
        )
    )
    old_membership = result.scalars().one()
    assert old_membership.ended_at is not None, "membership in the abandoned leaf group must be closed"

    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == person.id,
            OrganizationMembership.organization_id == new_leaf.id,
        )
    )
    new_membership = result.scalars().one()
    assert new_membership.ended_at is None

    # The shared ancestor "Gp-EI1" is still active either way.
    result = await db_session.execute(select(Organization).where(Organization.name == "Gp-EI1"))
    root = result.scalars().one()
    result = await db_session.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.person_id == person.id,
            OrganizationMembership.organization_id == root.id,
        )
    )
    root_membership = result.scalars().one()
    assert root_membership.ended_at is None
