from datetime import datetime

import pytest
from sqlalchemy import select

from db.models import ExternalIdentity, Organization, OrganizationMembership, Person
from palantint_scripts.loaders import groupes as groupes_loader
from palantint_scripts.loaders import trombint as trombint_loader
from palantint_scripts.scrapers import groupes as groupes_scraper
from palantint_scripts.db_helpers import get_or_create_organization, sync_membership

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


def test_parse_hierarchy_chain_keeps_course_prefixes_separate():
    assert groupes_loader.parse_hierarchy_chain("Gp-EI2-SIC4103-G1") == [
        "Gp-EI2", "Gp-EI2-SIC4103", "Gp-EI2-SIC4103-G1",
    ]
    assert groupes_loader.parse_hierarchy_chain("Gp-EI2-CIA4101") == [
        "Gp-EI2", "Gp-EI2-CIA4101",
    ]


def test_group_discovery_uses_complete_course_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(groupes_scraper, "SCRAPS_AUTO_DIR", tmp_path)
    agenda_dir = tmp_path / "agenda"
    agenda_dir.mkdir()
    now = datetime.now().isoformat()
    write_json(agenda_dir, "index.json", {
        "PRJ1": {"last_sync": now},
        "USR1": {"last_sync": now},
    })
    write_json(agenda_dir, "scope.json", {"status": "complete", "calendar_ids": ["PRJ1"]})
    write_json(agenda_dir, "PRJ1.json", [{"groups": ["Gp-COURSE"], "group_ids": {"Gp-COURSE": "123"}}])
    write_json(agenda_dir, "USR1.json", [{"groups": ["Gp-PRIVATE"], "group_ids": {"Gp-PRIVATE": "456"}}])

    ids, labels = groupes_scraper._agenda_group_targets()
    assert ids == {"123": "Gp-COURSE"}
    assert labels == {"gpcourse"}


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
async def test_load_groupes_creates_roster_only_student_by_stable_id(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "groupes.json", [
        {"name": "Gp-EI2-G1", "members": [{"id_objet": "41581", "name": "RIVIÈRE Florian"}]},
    ])
    await groupes_loader.load_groupes(db_session)
    await groupes_loader.load_groupes(db_session)

    people = (await db_session.execute(select(Person))).scalars().all()
    assert len(people) == 1
    assert (people[0].first_name, people[0].last_name) == ("Florian", "RIVIÈRE")
    identities = (await db_session.execute(select(ExternalIdentity))).scalars().all()
    assert len(identities) == 1
    assert identities[0].external_id == "41581"


@pytest.mark.asyncio
async def test_complete_roster_scope_closes_old_scraped_groups_only(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(tmp_path))
    write_json(tmp_path, "groupes.json", [
        {"id": "1", "name": "Gp-EI1-G1", "members": [{"id_objet": "100", "name": "DOE John"}]},
    ])
    await groupes_loader.load_groupes(db_session)
    john = (await db_session.execute(select(Person))).scalars().one()
    manual = await get_or_create_organization(db_session, kind="CLASS_GROUP", name="Manual group")
    await sync_membership(db_session, john.id, manual.id, source_code="vault_manual")

    write_json(tmp_path, "groupes.json", [
        {"id": "2", "name": "Gp-EI1-G2", "members": [{"id_objet": "101", "name": "MARTIN Alice"}]},
    ])
    write_json(tmp_path, "groupes_scope.json", {"status": "complete", "target_ids": ["2"]})
    await groupes_loader.load_groupes(db_session)

    rows = (await db_session.execute(
        select(Organization.name, OrganizationMembership.ended_at)
        .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
        .where(OrganizationMembership.person_id == john.id)
    )).all()
    by_name = dict(rows)
    assert by_name["Gp-EI1-G1"] is not None
    assert by_name["Manual group"] is None


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
