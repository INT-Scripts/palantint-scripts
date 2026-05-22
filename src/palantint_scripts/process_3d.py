import os
import json
import zipfile
import hashlib
import numpy as np
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent.parent.parent
# Check both 'imput_gltf' and 'input_gltf' because of the typo in the folder name
INPUT_DIR = ROOT_DIR / "data" / "scraps" / "imput_gltf"
if not INPUT_DIR.exists():
    INPUT_DIR = ROOT_DIR / "data" / "scraps" / "input_gltf"
TEMP_DIR = ROOT_DIR / "data" / "scraps" / "processing_temp"
OUTPUT_DIR = ROOT_DIR / "data" / "assets" / "3d"

def get_tile_fingerprint(gltf_path):
    """Generates a geometric fingerprint based on the local bounding box."""
    with open(gltf_path, 'r') as f:
        data = json.load(f)
    # Search for the POSITION accessor (VEC3 with max/min)
    for acc in data.get("accessors", []):
        if acc.get("type") == "VEC3" and "max" in acc and "min" in acc:
            # Create a stable string representation of the bbox
            # Rounding to 4 decimal places to handle floating point jitter
            bbox = [round(v, 4) for v in (acc["max"] + acc["min"])]
            return tuple(bbox)
    return None

def process_3d_assets():
    print("🚀 Starting 3D Asset Pipeline...")
    
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    zip_files = list(INPUT_DIR.glob("*.zip"))
    if not zip_files:
        print(f"❌ No zip files found in {INPUT_DIR}")
        return

    sets = []

    # 1. Extraction & Analysis
    for zip_path in zip_files:
        set_name = zip_path.stem
        extract_path = TEMP_DIR / set_name
        print(f"📦 Extracting {set_name}...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)
        
        gltf_files = list(extract_path.rglob("*.gltf"))
        tile_data = []
        for g in gltf_files:
            fingerprint = get_tile_fingerprint(g)
            if fingerprint is None:
                print(f"⚠️ Warning: Could not fingerprint {g}")
                continue

            with open(g, 'r') as f:
                data = json.load(f)
            
            matrix = data["nodes"][0].get("matrix")
            bin_uri = data["buffers"][0]["uri"]
            bin_path = g.parent / bin_uri
            
            if not bin_path.exists():
                print(f"⚠️ Warning: Bin {bin_uri} not found for {g}")
                continue
                
            tile_data.append({
                "gltf_path": g,
                "bin_path": bin_path,
                "fingerprint": fingerprint,
                "matrix": np.array(matrix) if matrix else None,
                "original_data": data
            })
        
        sets.append({
            "name": set_name,
            "tiles": tile_data,
            "offset": np.zeros(3)
        })

    if not sets:
        print("❌ No valid tile sets found.")
        return

    # 2. Sequential Alignment
    anchor_set = sets[0]
    aligned_sets = [anchor_set]
    to_align = sets[1:]

    # Map of fingerprint -> global matrix
    global_tile_map = {t["fingerprint"]: t["matrix"] for t in anchor_set["tiles"] if t["matrix"] is not None}

    while to_align:
        progress_made = False
        remaining = []
        
        for s in to_align:
            common_tiles = [t for t in s["tiles"] if t["fingerprint"] in global_tile_map]
            
            if common_tiles:
                offsets = []
                for t in common_tiles:
                    m_anchor = global_tile_map[t["fingerprint"]]
                    m_current = t["matrix"]
                    if m_anchor is not None and m_current is not None:
                        offsets.append(m_anchor[12:15] - m_current[12:15])
                
                if offsets:
                    s["offset"] = np.mean(offsets, axis=0)
                    print(f"🔗 Aligned {s['name']} using {len(common_tiles)} common tiles. Offset: {s['offset']}")
                    
                    for t in s["tiles"]:
                        if t["fingerprint"] not in global_tile_map and t["matrix"] is not None:
                            m_global = t["matrix"].copy()
                            m_global[12:15] += s["offset"]
                            global_tile_map[t["fingerprint"]] = m_global
                    
                    aligned_sets.append(s)
                    progress_made = True
                else:
                    remaining.append(s)
            else:
                remaining.append(s)
        
        if not progress_made and remaining:
            print(f"⚠️ Warning: Could not find common tiles for {len(remaining)} sets. They will be placed at original coords.")
            for s in remaining:
                aligned_sets.append(s)
            break
        to_align = remaining

    # 3. Export & Deduplication
    final_unique_tiles = {} # fingerprint -> tile_info
    
    for s in aligned_sets:
        for t in s["tiles"]:
            if t["fingerprint"] not in final_unique_tiles:
                if t["matrix"] is not None:
                    t["matrix"][12:15] += s["offset"]
                    t["original_data"]["nodes"][0]["matrix"] = t["matrix"].tolist()
                final_unique_tiles[t["fingerprint"]] = t

    # 4. Final Write
    sorted_tiles = list(final_unique_tiles.values())
    print(f"💾 Writing {len(sorted_tiles)} unique aligned tiles to {OUTPUT_DIR}...")
    
    for idx, t in enumerate(sorted_tiles, 1):
        new_name = f"tile_{idx}"
        gltf_out = OUTPUT_DIR / f"{new_name}.gltf"
        bin_out = OUTPUT_DIR / f"{new_name}.bin"
        
        t["original_data"]["buffers"][0]["uri"] = f"{new_name}.bin"
        with open(gltf_out, 'w') as f:
            json.dump(t["original_data"], f, indent=2)
        shutil.copy(t["bin_path"], bin_out)

    print("✅ 3D Pipeline Complete.")

if __name__ == "__main__":
    process_3d_assets()
