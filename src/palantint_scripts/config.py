import os
from pathlib import Path

# Base Paths
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
DATA_DIR = BASE_DIR / "data"
SCRAPS_DIR = DATA_DIR / "scraps"
SCRAPS_AUTO_DIR = SCRAPS_DIR / "auto"
SCRAPS_MANUAL_DIR = SCRAPS_DIR / "manual"
EXPORTS_DIR = DATA_DIR / "exports"
ASSETS_DIR = DATA_DIR / "assets"
PLANS_DIR = ASSETS_DIR / "plans"
PRIVATE_ASSETS_DIR = DATA_DIR / "private_assets"
WEMAP_DIR = SCRAPS_AUTO_DIR / "wemap"
WEMAP_IMPORT_DIR = SCRAPS_MANUAL_DIR / "wemap"
GEO_PLANS_DIR = PRIVATE_ASSETS_DIR / "maps"
# Campus map context: raw harvests are archived under SCRAPS_AUTO_DIR and can be
# rebuilt offline; only the published products are served from GEO_PLANS_DIR.
BASEMAP_ARCHIVE_DIR = SCRAPS_AUTO_DIR / "basemap"
IGN_ARCHIVE_DIR = SCRAPS_AUTO_DIR / "ign"
BASEMAP_PATH = GEO_PLANS_DIR / "basemap.geojson"
AERIAL_DIR = GEO_PLANS_DIR / "aerial"
RELIEF_DIR = GEO_PLANS_DIR / "relief"
PLAN_REGISTRATION_PATH = SCRAPS_AUTO_DIR / "plans" / "plan-registration.json"

INPUT_PNGS_DIR = SCRAPS_MANUAL_DIR / "input_pngs"

def ensure_dirs():
    """Ensure essential directories exist."""
    SCRAPS_AUTO_DIR.mkdir(parents=True, exist_ok=True)
    SCRAPS_MANUAL_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_PNGS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    PLANS_DIR.mkdir(parents=True, exist_ok=True)

ensure_dirs()
