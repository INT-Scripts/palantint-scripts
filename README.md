# 🛠️ PalantINT Scripts: Technical Manual

This directory contains the **PalantINT Synchronization Engine**, a decoupled ETL (Extract, Transform, Load) pipeline designed for high-performance campus data ingestion and OSINT (Open Source Intelligence) preservation.

---

## ⚙️ Installation & First-Time Setup

```bash
# 1. Install Python dependencies
uv sync

# 2. Install Playwright browser binaries (required for Group scraper — one-time step)
#    The Playwright Python package does NOT bundle browser executables.
uv run playwright install chromium

# 3. Configure CAS credentials (optional — pipeline will prompt if not set)
#    Edit the root .env file and fill in:
#    CAS_USERNAME=your_login
#    CAS_PASSWORD=your_password

# 4. Run the interactive pipeline
uv run palantint
```

> **Note**: Step 2 downloads ~130 MB of Chromium browser binaries used by the Group Roster
> scraper to navigate JavaScript-heavy SI-Étudiants pages. It is a one-time setup per machine.

---


## 🏗️ Architecture: The Decoupled Pipeline

PalantINT intentionally separates data acquisition from database management to ensure system resilience and offline capability.

1.  **Phase 1: Harvest (Scrapers)**: Connects to external servers (CAS, Agenda, etc.) and saves raw data as JSON in `data/scraps/auto/` (manual input sources reside in `data/scraps/manual/`). Scrapers are parallelized.
2.  **Phase 2: Ingest (Loaders)**: Synchronizes local JSON files into PostgreSQL. Loaders are sequential and follow a strict **3-Pass Sequence** to ensure identity stability.
3.  **Phase 3: Snapshot (Vault)**: Backs up manual research (Notes, Socials, Relationships) from the DB into portable JSON in `data/exports/`.

### 📌 Path Resolution Contract
All scripts import path locations from `palantint_scripts.config`:
* `SCRAPS_AUTO_DIR` (`data/scraps/auto/`): `agenda/`, `clubs.json`, `groupes.json`, `logements.json`, `students.json`, `processing_temp/`
* `SCRAPS_MANUAL_DIR` (`data/scraps/manual/`): `apartments.csv/txt`, `foyer_map.csv`, `input_svgs/`, `input_gltf/`, `metadata/`, `compare/`
* `EXPORTS_DIR` (`data/exports/`) & `ASSETS_DIR` (`data/assets/`)

---

## 🚀 The Synchronization Engine (`sync.py`)

The `sync.py` script acts as the **Orchestrator**. It manages:
- **TUI Interface**: Professional natural-language interaction via `questionary`.
- **Navigation**: State-driven wizard with fluid "Left Arrow/Esc" back-navigation.
- **Authentication**: Centralized CAS login with a persistent retry loop.
- **Dependency Injection**: Automatic passing of DB sessions and CAS clients to sub-modules.
- **Unified Configuration**: Centralized `config` dictionary for all scrapers.
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
Add an entry to the `PIPELINE_DOMAINS` list in `sync.py`.

### 2. Implement the Scraper
Create `src/palantint_scripts/scrapers/domain.py`. 

**Requirement**: Scrapers should use the provided `config` dictionary for rate-limiting.

```python
async def scrape_domain(cas_client, progress, task_id, config, log):
    delay = config.get("delay", 0.2)
    concurrency = config.get("concurrency", 5)
    # Perform work using delay/concurrency
```

### 3. Implement the Loader
Create `src/palantint_scripts/loaders/domain.py`. 

**Requirement**: Use `db_session.merge()` for all updates to support the Identity Anchor system.

---

## 💉 Parameter Injection Reference

The engine inspects your function signature and automatically provides the following variables:

| Parameter | Type | Description |
|-----------|------|-------------|
| `config` | `dict` | Unified configuration (delay, concurrency, hydrate, etc.). |
| `cas_client` | `CASClient` | Authenticated session for web scraping. |
| `db_session` | `AsyncSession` | Active SQLAlchemy session. |
| `progress` | `Progress` | Rich progress bar object. |
| `task_id` | `int` | The ID of the specific sub-task bar. |
| `log` | `Callable` | Function to print to the unified CLI log. |

---

## 🎨 UI & UX Standards

- **Geometry**: All CLI panels use `box.HEAVY` or `box.ROUNDED`.
- **Navigation**: Support `Left Arrow` and `Escape` for intuitive back-navigation.
- **Language**: Use **Natural Language** (e.g., "Synchronizing..." instead of "Executing task...").
- **Error Reporting**: Modules **must** raise exceptions on failure to trigger rollbacks.
