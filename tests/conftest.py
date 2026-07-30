import json

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import palantint_scripts.db_helpers as db_helpers
from db.seed import seed_default_data

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    """Fresh in-memory SQLite schema + seeded DataSource/Organization rows
    per test, mirroring sync.py's real pipeline order (seed_default_data runs
    before any loader). `db_helpers._data_source_cache` is a process-global
    dict keyed by source code -> uuid; since every test rebuilds the schema
    from scratch, a stale cache entry from a previous test would point at a
    DataSource row that no longer exists in THIS test's DB, so it must be
    cleared per-test (purely a test-isolation concern; in the real app
    there's only ever one DB per process, so the cache is safe there)."""
    db_helpers._data_source_cache.clear()

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as session:
        await seed_default_data(session, log=lambda x: None)
        await session.commit()
        yield session

    await engine.dispose()


def write_json(directory, filename, data):
    path = directory / filename
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)
