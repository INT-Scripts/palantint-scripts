# PalantINT Scripts 🎭

Comprehensive OSINT and data ingestion pipeline for the PalantINT project.

## Unified Pipeline
The scripts now use a shared `CASClient` and asynchronous networking for maximum efficiency.

### Installation
```bash
uv sync
```

### Usage
#### 🎭 Master Pipeline (Scraping)
Run all data synchronization steps with a granular progress UI:
```bash
uv run palantint-scrape
```
Add `--purge` to wipe the DB first.

#### 🔑 Admin Setup
```bash
uv run palantint-admin <user> <pass>
```

#### 🚀 Blooket Bot Swarm
```bash
uv run palantint-blooket <GAME_CODE> <NAME>
```

#### 🖨️ Campus Printing
```bash
uv run palantint-print list-webprint
uv run palantint-print auto my_document.pdf
```

## Integrated Packages
This project integrates logic from:
- `cas-connector`: Unified CAS authentication.
- `trombint`: Student directory scraping.
- `si-agenda`: Timetable synchronization.
- `tsprint`: PaperCut printing integration.
- `blooket-int`: Performance bot swarm.
- `palantint-core`: Shared DB models.
