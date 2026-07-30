"""Postgres-backed tests for `palantint_scripts.loaders.clubs` and a full,
independent ordered-pipeline idempotency test.

These require real Postgres (loaders/clubs.py uses
`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)`,
which SQLite cannot execute), so the whole module spins up a throwaway
`postgres:18-alpine` Docker container per test session and tears it down
afterwards. It never touches the real `data/exports/`, `data/scraps/`, or
the project's running `palantint-db-1` container.

Run (from `scripts/`):
    PYTHONPATH=src:../backend/src .venv/bin/python -m pytest tests/test_clubs_loader_postgres.py -q
"""
import json
import shutil
import socket
import subprocess
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from db.models import (
    Event,
    EventOrganization,
    IngestionRun,
    Organization,
    OrganizationLink,
    OrganizationMembership,
    Person,
    PersonHousing,
)
from db.seed import seed_default_data
import palantint_scripts.db_helpers as db_helpers
from palantint_scripts.loaders import clubs as clubs_loader
from palantint_scripts.loaders import trombint as trombint_loader
from palantint_scripts.loaders import groupes as groupes_loader
from palantint_scripts.loaders import agenda as agenda_loader
from palantint_scripts.loaders import apartments as apartments_loader
from palantint_scripts.loaders import vault as vault_loader

pytestmark = pytest.mark.skipif(not shutil.which("docker"), reason="docker not available")

POSTGRES_IMAGE = "postgres:18-alpine"
PG_USER = "test"
PG_PASSWORD = "test"
PG_DB = "palantint_test"


def _free_port() -> int:
    """Grab an OS-assigned free TCP port to avoid colliding with any other
    concurrently-running throwaway containers (e.g. from a parallel test
    agent), or with the real dev stack's palantint-db-1 (bound to 5432)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_pg_ready(container_name: str, timeout: float = 60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "exec", container_name, "pg_isready", "-U", PG_USER, "-d", PG_DB],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Postgres container '{container_name}' did not become ready within {timeout}s")


@pytest_asyncio.fixture(scope="module")
async def pg_url():
    """Starts one throwaway Postgres container for this whole test module and
    tears it down afterwards, even on failure."""
    name = f"palantint-clubtest-{uuid.uuid4().hex[:10]}"
    port = _free_port()
    subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-e", f"POSTGRES_USER={PG_USER}",
            "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
            "-e", f"POSTGRES_DB={PG_DB}",
            "-p", f"{port}:5432",
            POSTGRES_IMAGE,
        ],
        check=True, capture_output=True,
    )
    try:
        _wait_pg_ready(name)
        yield f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@localhost:{port}/{PG_DB}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest_asyncio.fixture
async def pg_session(pg_url):
    """Fresh schema (drop_all/create_all) + seeded default data + a clean
    `db_helpers` DataSource-id cache for every test. The cache is a
    process-global dict keyed by source code -> uuid; since every test here
    rebuilds the schema from scratch, a stale cache entry from a previous
    test would point at a DataSource row that no longer exists and blow up
    with a bogus FK error, so it must be cleared per-test (this is purely a
    test-isolation concern -- in the real app there's only ever one DB per
    process, so the cache is safe there)."""
    db_helpers._data_source_cache.clear()

    engine = create_async_engine(pg_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await seed_default_data(session)
        await session.commit()
        yield session

    await engine.dispose()


def _write_json(dir_path, filename, data):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── Part 1: loaders/clubs.py direct tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_load_clubs_missing_file_is_noop(pg_session, tmp_path, monkeypatch):
    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(tmp_path))  # no clubs.json written
    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(IngestionRun))
    assert result.scalars().all() == []
    # seed_default_data already populates base SCHOOL/PROMO Organization rows
    # -- load_clubs must not touch those or create any CLUB/BUREAU rows when
    # there's nothing to sync.
    result = await pg_session.execute(select(Organization).where(Organization.kind.in_(["CLUB", "BUREAU"])))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_clubs_upsert_idempotent_no_duplicate_and_fields_update(pg_session, tmp_path, monkeypatch):
    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(tmp_path))
    clubs = [{
        "name": "Club Robotique",
        "type": "Club",
        "slug": "robotique",
        "description": "v1 description",
        "logo_url": "https://logo/v1.png",
        "color_primary": "#111111",
        "color_secondary": "#222222",
        "association_of_origin": "BDE",
        "links": [{"name": "Site", "url": "https://robotique.example/v1"}],
    }]
    _write_json(tmp_path, "clubs.json", clubs)

    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(Organization).where(Organization.name == "Club Robotique"))
    orgs = result.scalars().all()
    assert len(orgs) == 1
    org_id = orgs[0].id
    assert orgs[0].description == "v1 description"
    assert orgs[0].logo_url == "https://logo/v1.png"

    # Re-sync the SAME club with changed fields -- must update the existing
    # row in place, not insert a duplicate Organization.
    clubs[0]["description"] = "v2 description, updated"
    clubs[0]["logo_url"] = "https://logo/v2.png"
    clubs[0]["color_primary"] = "#333333"
    _write_json(tmp_path, "clubs.json", clubs)

    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()
    # load_clubs writes via a raw Core `insert(...).on_conflict_do_update(...)`
    # statement, which updates the row at the DB level but does NOT refresh
    # any already-identity-mapped ORM object for that row in this session
    # (that's standard SQLAlchemy behavior, not a loader bug) -- expire so the
    # next select actually re-reads the updated columns instead of returning
    # the stale in-memory copy from the first select above.
    pg_session.expire_all()

    result = await pg_session.execute(select(Organization).where(Organization.name == "Club Robotique"))
    orgs = result.scalars().all()
    assert len(orgs) == 1, "second sync of the same club created a duplicate Organization row"
    assert orgs[0].id == org_id, "second sync replaced the row's identity instead of updating it in place"
    assert orgs[0].description == "v2 description, updated"
    assert orgs[0].logo_url == "https://logo/v2.png"
    assert orgs[0].color_primary == "#333333"


@pytest.mark.asyncio
async def test_load_clubs_kind_mapping(pg_session, tmp_path, monkeypatch):
    # Real MiNET org.type values, confirmed from a live scrape
    # (data/scraps/auto/clubs.json): "club", "liste", "association",
    # "administration" — never "Bureau"/"Club" (the old, incorrect
    # assumption this test used to encode).
    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(tmp_path))
    clubs = [
        {"name": "BDE", "type": "association"},
        {"name": "Club Photo", "type": "club"},
        {"name": "Hypnos", "type": "liste"},
        {"name": "Organisation Supprimée", "type": "administration"},
        {"name": "Club Mystere", "type": None},
        {"name": "Club Autre Type", "type": "SomethingElse"},
    ]
    _write_json(tmp_path, "clubs.json", clubs)

    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(Organization))
    kinds_by_name = {o.name: o.kind for o in result.scalars().all()}

    assert kinds_by_name["BDE"] == "BUREAU"
    assert kinds_by_name["Club Photo"] == "CLUB"
    assert kinds_by_name["Hypnos"] == "CLUB"  # "liste" is still a club, just informal
    assert kinds_by_name["Organisation Supprimée"] == "ADMIN"  # never surfaced as a club
    assert kinds_by_name["Club Mystere"] == "CLUB"  # missing type defaults to CLUB
    assert kinds_by_name["Club Autre Type"] == "CLUB"  # unrecognized type also defaults to CLUB


@pytest.mark.asyncio
async def test_load_clubs_links_fully_replaced_not_accumulated(pg_session, tmp_path, monkeypatch):
    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(tmp_path))
    clubs = [{
        "name": "Club Lien",
        "type": "Club",
        "links": [
            {"name": "Site", "url": "https://a.example"},
            {"name": "Insta", "url": "https://insta.example/a"},
        ],
    }]
    _write_json(tmp_path, "clubs.json", clubs)
    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(Organization).where(Organization.name == "Club Lien"))
    org = result.scalars().one()
    result = await pg_session.execute(select(OrganizationLink).where(OrganizationLink.organization_id == org.id))
    links = result.scalars().all()
    assert len(links) == 2

    # Re-sync with a different, smaller link set -- old links must be fully
    # replaced, not accumulated alongside the new ones.
    clubs[0]["links"] = [{"name": "Discord", "url": "https://discord.example/b"}]
    _write_json(tmp_path, "clubs.json", clubs)
    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(OrganizationLink).where(OrganizationLink.organization_id == org.id))
    links = result.scalars().all()
    assert len(links) == 1, "old links weren't replaced -- they accumulated across syncs"
    assert links[0].name == "Discord"
    assert links[0].url == "https://discord.example/b"

    # Re-sync with no links at all -- must clear them, not leave stale rows.
    clubs[0]["links"] = []
    _write_json(tmp_path, "clubs.json", clubs)
    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(OrganizationLink).where(OrganizationLink.organization_id == org.id))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_load_clubs_creates_ingestion_run_with_status_and_counts(pg_session, tmp_path, monkeypatch):
    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(tmp_path))
    clubs = [
        {"name": "Club Un", "type": "Club"},
        {"name": "Club Deux", "type": "Bureau"},
    ]
    _write_json(tmp_path, "clubs.json", clubs)

    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()

    result = await pg_session.execute(select(IngestionRun))
    runs = result.scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "SUCCESS"
    assert run.finished_at is not None
    # NB (pinned current behavior, not fixed): load_clubs always reports its
    # tally as `records_updated`, even for brand-new Organization rows -- it
    # never populates `records_created`. Worth a product call on whether
    # created-vs-updated should be tracked separately here; not changed as
    # part of this audit.
    assert run.records_created == 0
    assert run.records_updated == 2
    assert run.records_deactivated == 0

    # A second sync opens a second, independent IngestionRun row rather than
    # reusing/overwriting the first (provenance history is preserved).
    await clubs_loader.load_clubs(pg_session)
    await pg_session.flush()
    result = await pg_session.execute(select(IngestionRun))
    assert len(result.scalars().all()) == 2


# ── Part 2: independent full-pipeline idempotency test ──────────────────────

def _seed_pipeline_fixtures(scraps_dir, agenda_dir, exports_dir):
    """Minimal, hand-crafted (not real-scrape) fixtures exercising the full
    clubs -> trombint -> groupes -> agenda -> apartments -> vault chain, per
    sync.py's PIPELINE_DOMAINS order."""
    _write_json(scraps_dir, "clubs.json", [
        {"name": "Club Pipeline", "type": "Club", "links": [{"name": "Site", "url": "https://pipeline.example"}]},
    ])

    _write_json(scraps_dir, "students.json", [
        {"uid": "u1", "nom_complet": "Jean DUPONT", "email": "jean.dupont@example.com", "promo": "Ingénieur 1ère année"},
        {"uid": "u2", "nom_complet": "Marie CURIE", "email": "marie.curie@example.com", "promo": "Ingénieur 1ère année"},
        {"uid": "u3", "nom_complet": "Paul MARTIN", "email": "paul.martin@example.com", "promo": "Management 1ère année"},
    ])

    _write_json(scraps_dir, "groupes.json", [
        {"name": "Gp-EI1-G1", "members": [{"name": "Jean Dupont"}, {"name": "Marie Curie"}]},
    ])

    _write_json(agenda_dir, "index.json", {
        "GP1": {"name": "Gp-EI1-G1 calendar", "event_count": 1},
    })
    _write_json(agenda_dir, "GP1.json", [
        {
            "id": "evt1", "name": "Cours Test", "type": "cours", "date": "2026-01-15",
            "start_time": "08h00", "end_time": "10h00", "room": "Amphi1",
            "groups": ["Gp-EI1-G1"], "professors": [],
        },
    ])

    _write_json(scraps_dir, "logements.json", {
        "1101": {
            "Bâtiment": "U3", "Etage": "1", "Type": "T1", "Superficie": "20",
            "Tarif": "400", "_req_b": 0, "_req_e": "",
        },
    })

    _write_json(exports_dir, "apartments.json", {"u1": "1101"})


async def _table_count(session, model):
    result = await session.execute(select(model))
    return len(result.scalars().all())


@pytest.mark.asyncio
async def test_full_pipeline_idempotent_across_two_runs(pg_session, tmp_path, monkeypatch):
    scraps_dir = tmp_path / "scraps"
    agenda_dir = scraps_dir / "agenda"
    exports_dir = tmp_path / "exports"
    _seed_pipeline_fixtures(scraps_dir, agenda_dir, exports_dir)

    monkeypatch.setattr(clubs_loader, "DATA_DIR", str(scraps_dir))
    monkeypatch.setattr(trombint_loader, "DATA_DIR", str(scraps_dir))
    monkeypatch.setattr(groupes_loader, "DATA_DIR", str(scraps_dir))
    monkeypatch.setattr(agenda_loader, "DATA_DIR", str(agenda_dir))
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(exports_dir))
    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", str(scraps_dir))
    monkeypatch.setattr(vault_loader, "EXPORT_DIR", str(exports_dir))

    async def run_pipeline_once():
        await clubs_loader.load_clubs(pg_session)
        await trombint_loader.load_trombint(pg_session)
        await groupes_loader.load_groupes(pg_session)
        await agenda_loader.load_agenda(pg_session)
        await apartments_loader.load_apartments(pg_session)
        await vault_loader.load_vault(pg_session)
        await pg_session.flush()
        await pg_session.commit()

    await run_pipeline_once()

    people_1 = await _table_count(pg_session, Person)
    memberships_1 = await _table_count(pg_session, OrganizationMembership)
    events_1 = await _table_count(pg_session, Event)
    housing_1 = await _table_count(pg_session, PersonHousing)
    event_orgs_1 = await _table_count(pg_session, EventOrganization)
    runs_1 = await _table_count(pg_session, IngestionRun)

    # Sanity on what the first run actually produced, so a silent no-op
    # pipeline can't pass this test vacuously.
    assert people_1 == 3
    assert memberships_1 == 11  # 3 PROMO (trombint) + 8 CLASS_GROUP (groupes: 3+3 for the Gp- group chain, 2 for Paul's promo fallback)
    assert events_1 == 1
    assert event_orgs_1 == 1
    assert housing_1 == 1
    assert runs_1 == 6  # one IngestionRun per domain that ran: clubs, trombint, groupes, agenda, apartments, vault

    # Re-run the exact same pipeline against the exact same fixture files a
    # second time. This must be idempotent: no duplicate people, memberships,
    # events, or housing rows -- only ingestion_runs, which records history
    # and should double.
    await run_pipeline_once()

    people_2 = await _table_count(pg_session, Person)
    memberships_2 = await _table_count(pg_session, OrganizationMembership)
    events_2 = await _table_count(pg_session, Event)
    housing_2 = await _table_count(pg_session, PersonHousing)
    event_orgs_2 = await _table_count(pg_session, EventOrganization)
    runs_2 = await _table_count(pg_session, IngestionRun)

    assert people_2 == people_1, "second pipeline run duplicated Person rows"
    assert memberships_2 == memberships_1, "second pipeline run duplicated OrganizationMembership rows"
    assert events_2 == events_1, "second pipeline run duplicated Event rows"
    assert event_orgs_2 == event_orgs_1, "second pipeline run duplicated EventOrganization rows"
    assert housing_2 == housing_1, "second pipeline run duplicated PersonHousing rows"
    assert runs_2 == runs_1 * 2, "ingestion_runs should record one new run per domain on every pipeline execution"

    # No active membership was closed out by the second identical run (no
    # accumulation of stale/duplicate concurrently-active memberships).
    result = await pg_session.execute(
        select(OrganizationMembership).where(OrganizationMembership.ended_at.is_(None))
    )
    active_memberships = result.scalars().all()
    assert len(active_memberships) == memberships_1, "an identical re-run closed out memberships that should still be active"
