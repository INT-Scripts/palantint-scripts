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
