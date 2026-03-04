# PalantINT Scripts 🎭

Comprehensive OSINT and data ingestion pipeline for the PalantINT project. 

## Structure 🏗️

- `pyproject.toml`: Project configuration and dependencies.
- `main.py`: Entry point for all scripts.
- `src/palantint_scripts/`: Package containing the scripts:
    - `scrape_all.py`: The master orchestration TUI.
    - `create_admin.py`: Utility to create an initial admin user.
    - `backfill.py`: Enrichment of student data.
    - `scrape_trombint.py`: Scraper for the student directory.
    - `scrape_clubs.py`: Scraper for student clubs.
    - `import_agenda.py`: Importer for agenda scraps.
    - `fix_images.py`: Utility to redownload missing profile pictures.
    - `svg_processor.py`: Vector processing for building plans.
    - `populate.py`: Seed database with default values.

## Installation 📦

It is recommended to use `uv`:

```bash
cd scripts/
uv sync
```

## Usage 🚀

All commands are accessible via the CLI:

### 🎭 Master Pipeline (with TUI)
Run the full scraping and synchronization sequence:
```bash
uv run palantint-scrape
```
Add `--purge` as an argument to wipe the database before scraping.

### 🔑 Create Admin User
```bash
uv run palantint-admin <username> <password>
```

### 🗺️ Process Building SVGs
Processes raw SVG building plans found in `data/scraps/input_svgs/` into active building plans in `data/assets/plans/`.
```bash
uv run palantint-svg
```

### 🐍 Legacy execution
You can also run through `main.py`:
```bash
uv run scripts/main.py
```

## Environment Variables 🔐

Ensure you have the following environment variables set if you need CAS-authenticated scraping:
- `CAS_USERNAME`
- `CAS_PASSWORD`
- `DATABASE_URL` (optional, defaults to local Docker instance)
