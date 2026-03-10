# 🛠️ PalantINT Scripts: Technical Manual

This directory contains the **PalantINT Synchronization Engine**, a decoupled ETL (Extract, Transform, Load) pipeline designed for high-performance campus data ingestion and OSINT (Open Source Intelligence) preservation.

---

## 🏗️ Architecture: The Decoupled Pipeline

PalantINT intentionally separates data acquisition from database management to ensure system resilience and offline capability.

1.  **Phase 1: Harvest (Scrapers)**: Connects to external servers (CAS, Agenda, etc.) and saves raw data as JSON in `data/scraps/`. Scrapers are parallelized.
2.  **Phase 2: Ingest (Loaders)**: Synchronizes local JSON files into PostgreSQL. Loaders are sequential and follow a strict **3-Pass Sequence** to ensure identity stability.
3.  **Phase 3: Snapshot (Vault)**: Backs up manual research (Notes, Socials, Relationships) from the DB into portable JSON in `data/exports/`.

---

## 🚀 The Synchronization Engine (`sync.py`)

The `sync.py` script acts as the **Orchestrator**. It manages:
- **TUI Interface**: Professional natural-language interaction via `questionary`.
- **Authentication**: Centralized CAS login with a persistent retry loop.
- **Dependency Injection**: Automatic passing of DB sessions and CAS clients to sub-modules.
- **Rate-Limiting**: User-controllable cooldowns and concurrent connection limits.
- **Atomic Integrity**: Global database `commit()` only fires if all selected tasks achieve "Done" status.

---

## 🧪 The "Identity Anchor" Strategy (Pass System)

To prevent UUID collisions and broken research ties, the Ingest phase (Update Database) follows a mandatory sequence:

*   **Pass 1: Anchor Identities**: Automatically restores `Student` and `Club` registries from `data/exports/`. This "pins" existing people to their permanent UUIDs.
*   **Pass 2: Domain Synchronization**: Processes user-selected scrap files. It updates anchored subjects and creates new UUIDs *only* for first-time entries.
*   **Pass 3: Restore Research**: Automatically restores the rest of the vault (Social Graph, Comms Logs). These bind perfectly to the UUIDs anchored in Pass 1.

---

## 🛠️ Implementing a New Pipeline Step

To add a new data domain, follow these steps:

### 1. Register the Domain
Add an entry to the `PIPELINE_DOMAINS` list in `sync.py`:

```python
{
    "id": "dining",
    "name": "Dining Hall",
    "scraper": {
        "name": "Harvesting Menus",
        "module": "palantint_scripts.scrapers.dining",
        "func": "scrape_menus",
        "needs_cas": True # Only if web access is needed
    },
    "loader": {
        "name": "Update Menus",
        "module": "palantint_scripts.loaders.dining",
        "func": "load_menus",
        "needs_db": True
    }
}
```

### 2. Implement the Scraper
Create `src/palantint_scripts/scrapers/dining.py`. 

**Requirement**: Use `try...finally` to ensure progress indices are saved even if the user hits `Ctrl+C`.

```python
async def scrape_menus(cas_client, progress, task_id, delay, concurrency, log):
    sem = asyncio.Semaphore(concurrency)
    try:
        # Perform work using sem and delay
        pass
    finally:
        # Save progress/index here
        _save_state()
```

### 3. Implement the Loader
Create `src/palantint_scripts/loaders/dining.py`. 

**Requirement**: Use `db_session.merge()` for all updates to support the Identity Anchor system. Raise exceptions on critical failures to trigger a pipeline rollback.

```python
async def load_menus(db_session, progress, task_id, log):
    # Load JSON from scraps
    # Use db_session.merge(obj)
    pass
```

---

## 💉 Parameter Injection Reference

The engine inspects your function signature and automatically provides the following variables:

| Parameter | Type | Description |
|-----------|------|-------------|
| `cas_client` | `CASClient` | Authenticated session for web scraping. |
| `db_session` | `AsyncSession` | Active SQLAlchemy session. |
| `progress` | `Progress` | Rich progress bar object. |
| `task_id` | `int` | The ID of the specific sub-task bar. |
| `delay` | `float` | The user-selected cooldown (seconds). |
| `concurrency` | `int` | Max simultaneous connections allowed. |
| `log` | `Callable` | Function to print to the unified CLI log. |
| `force_reverify` | `bool` | True if a deep sync was requested. |

---

## 🎨 UI & UX Standards

- **Geometry**: All CLI panels use `box.HEAVY` or `box.ROUNDED`.
- **Language**: Use **Natural Language**.
    - ❌ `Loading Table StudentClub...`, `Fragmentation Mode`
    - ✅ `Synchronizing Organization Ties...`, `Update Everything`
- **Error Reporting**: Modules **must** raise exceptions on failure. The master script will catch them and display the `❌ Failed` status in the summary.
