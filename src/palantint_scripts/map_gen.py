import glob
import re
import os
import csv
import cv2
import numpy as np
import svgpathtools
import json

def get_svg_size(content):
    vw, vh = 1000, 1000
    w_m = re.search(r'viewBox="0 0 (\d+) (\d+)"', content)
    if w_m:
        vw, vh = int(w_m.group(1)), int(w_m.group(2))
    else:
        width_m = re.search(r'width="([\d.]+)"', content)
        height_m = re.search(r'height="([\d.]+)"', content)
        if width_m: vw = int(float(width_m.group(1)))
        if height_m: vh = int(float(height_m.group(1)))
    return vw, vh

from palantint_scripts.config import SCRAPS_MANUAL_DIR, BASE_DIR, PLANS_DIR

def process_foyer_map_csv(root_dir=None):
    csv_path = str(SCRAPS_MANUAL_DIR / "foyer_map.csv")
    if not os.path.exists(csv_path):
        return

    foyer_entries = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            room_id = row.get("room_id", "").strip()
            club_name = row.get("club_name", "").strip()
            if room_id:
                floor = "0" if room_id.startswith("F0") else ("1" if room_id.startswith("F1") else "0")
                foyer_entries[room_id] = {
                    "room_id": room_id,
                    "raw_name": club_name,
                    "club_name": club_name,
                    "floor": floor,
                    "building": "Foyer"
                }

    root_dir = str(BASE_DIR)
    out_dirs = [
        os.path.join(root_dir, "data", "assets", "clubs"),
        os.path.join(root_dir, "frontend", "public", "api", "assets", "clubs")
    ]

    for d in out_dirs:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "foyer_map.json"), "w", encoding="utf-8") as f:
            json.dump(foyer_entries, f, indent=4, ensure_ascii=False)

    # Also copy the raw CSV into data/assets/clubs — that directory is the
    # only one bind-mounted into the backend container (as ASSETS_DIR), while
    # data/scraps/manual is not, so the public /foyer/map endpoint reads it
    # from there rather than from the source-of-truth location.
    assets_clubs_csv = os.path.join(root_dir, "data", "assets", "clubs", "foyer_map.csv")
    with open(csv_path, "r", encoding="utf-8") as src, open(assets_clubs_csv, "w", encoding="utf-8") as dst:
        dst.write(src.read())

    print(f"  → Processed foyer_map.csv ({len(foyer_entries)} room mappings exported to foyer_map.json)")

def main():
    ROOT_DIR = str(BASE_DIR)
    INPUT_DIR = str(SCRAPS_MANUAL_DIR / "input_svgs")
    OUTPUT_DIRS = [
        str(PLANS_DIR)
    ]

    for d in OUTPUT_DIRS:
        os.makedirs(d, exist_ok=True)
    
    svgs = sorted(glob.glob(os.path.join(INPUT_DIR, "*.svg")))
    
    if not svgs:
        print(f"No SVGs found in {INPUT_DIR}")
        return

    print(f"Starting headless SVG processing for {len(svgs)} files...")

    EMBEDDED_STYLE = """<style id="plan-theme-styles">
  :root {
    --wall-stroke: #475569;
    --room-label-fill: #1e293b;
    --room-default-fill: rgba(120, 113, 108, 0.12);
    --room-default-stroke: rgba(120, 113, 108, 0.4);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --wall-stroke: #e2e8f0;
      --room-label-fill: #f8fafc;
    }
  }
  .dark {
    --wall-stroke: #e2e8f0;
    --room-label-fill: #f8fafc;
  }

  path.plan-wall {
    stroke: var(--wall-stroke);
    stroke-width: 2px;
    fill: none;
  }

  a[data-room] { cursor: pointer; }
  a[data-room] text, a[data-room] tspan { pointer-events: none !important; user-select: none !important; }
  
  /* 1. Default room fill & stroke */
  a[data-room] .room-area, g[data-room] .room-area, polygon.room-area, path.room-area, rect.room-area {
    fill: var(--room-default-fill);
    stroke: var(--room-default-stroke);
    stroke-width: 1px;
    pointer-events: all;
    transition: fill 0.15s ease, stroke 0.15s ease;
  }

  /* 2. Passive Occupied & Filtered room glow (Orange) */
  a[data-room][data-occupied="true"] .room-area, g[data-room][data-occupied="true"] .room-area,
  a[data-room][data-filtered="true"] .room-area, g[data-room][data-filtered="true"] .room-area {
    fill: rgba(249, 115, 22, 0.32) !important;
    stroke: #f97316 !important;
    stroke-width: 1.5px !important;
  }

  /* 3. Missing Metadata (Rose) */
  a[data-room][data-no-meta="true"] .room-area, g[data-room][data-no-meta="true"] .room-area {
    fill: rgba(244, 63, 94, 0.20) !important;
    stroke: #f43f5e !important;
    stroke-width: 1.5px !important;
  }
  a[data-room][data-no-meta="true"] .room-label {
    fill: #f43f5e !important;
  }

  /* 4. Selected / Active room (Blue Filled) */
  a[data-room][data-active="true"] .room-area, g[data-room][data-active="true"] .room-area,
  a[data-room][data-selected="true"] .room-area, g[data-room][data-selected="true"] .room-area {
    fill: rgba(37, 99, 235, 0.75) !important;
    stroke: #2563eb !important;
    stroke-width: 2.5px !important;
  }
  a[data-room][data-active="true"] .room-label, a[data-room][data-selected="true"] .room-label {
    fill: #ffffff !important;
    font-weight: bold;
  }

  /* 5. Hover state (Blue) — TOP PRIORITY (Overrides passive orange/rose, active, default) */
  a[data-room]:hover .room-area, a[data-room][data-hover="true"] .room-area,
  a[data-room][data-occupied="true"]:hover .room-area, a[data-room][data-occupied="true"][data-hover="true"] .room-area,
  a[data-room][data-no-meta="true"]:hover .room-area, a[data-room][data-no-meta="true"][data-hover="true"] .room-area,
  a[data-room][data-active="true"]:hover .room-area, a[data-room][data-active="true"][data-hover="true"] .room-area {
    fill: rgba(59, 130, 246, 0.55) !important;
    stroke: #3b82f6 !important;
    stroke-width: 2.5px !important;
    cursor: pointer !important;
  }
  a[data-room]:hover .room-label, a[data-room][data-hover="true"] .room-label {
    fill: #ffffff !important;
    font-weight: bold;
  }
  .room-label {
    fill: var(--room-label-fill);
  }
</style>"""

    for f in svgs:
        basename = os.path.basename(f)
        print(f"  → Processing {basename}")
        
        with open(f, 'r') as file:
            content = file.read()
            
        w, h = get_svg_size(content)
        
        # Pre-process image for flood filling
        scale = 2.0
        img = np.zeros((int(h*scale)+10, int(w*scale)+10), dtype=np.uint8)
        thickness = 6
        
        paths = re.findall(r'<path[^>]*d="([^"]+)"', content)
        for d in paths:
            try:
                path = svgpathtools.parse_path(d)
                for seg in path:
                    if type(seg) == svgpathtools.path.Line:
                        p1 = (int(seg.start.real * scale), int(seg.start.imag * scale))
                        p2 = (int(seg.end.real * scale), int(seg.end.imag * scale))
                        cv2.line(img, p1, p2, 255, thickness)
                    else:
                        num_points = max(2, int(seg.length() * scale / 2))
                        pts = [seg.point(t) for t in np.linspace(0, 1, num_points)]
                        cv_pts = np.array([[[int(p.real * scale), int(p.imag * scale)]] for p in pts], dtype=np.int32)
                        cv2.polylines(img, [cv_pts], False, 255, thickness=thickness)
            except: pass

        def process_match(m):
            full_text_tag = m.group(0)
            room_id = m.group(1)
            if not re.match(r'^[A-Za-z0-9_-]+$', room_id): return full_text_tag
            
            # Extract basic coordinates
            x_match = re.search(r'x="([-\d.]+)"', full_text_tag)
            y_match = re.search(r'y="([-\d.]+)"', full_text_tag)
            if not x_match or not y_match: return full_text_tag
            
            x, y = float(x_match.group(1)), float(y_match.group(1))
            cx, cy = int(x * scale), int((y - 4) * scale)
            
            # Clean text tag to avoid redefined attributes (fill, class, pointer-events)
            cleaned_text = re.sub(r'\sfill=["\'][^"\']*["\']', '', full_text_tag)
            cleaned_text = re.sub(r'\sclass=["\'][^"\']*["\']', '', cleaned_text)
            cleaned_text = re.sub(r'\spointer-events=["\'][^"\']*["\']', '', cleaned_text)
            
            # Re-inject our required attributes
            modified_text = re.sub(r'<text ', r'<text class="room-label" pointer-events="none" ', cleaned_text)
            
            mask = np.zeros((img.shape[0]+2, img.shape[1]+2), dtype=np.uint8)
            cx, cy = max(0, min(cx, img.shape[1]-1)), max(0, min(cy, img.shape[0]-1))
            nudge_directions = [(0,0), (0,-5), (0,5), (5,0), (-5,0), (5,-5), (-5,5), (5,5), (-5,-5)]
            filled = False
            for dx, dy in nudge_directions:
                tcx, tcy = cx + dx, cy + dy
                if 0 <= tcx < img.shape[1] and 0 <= tcy < img.shape[0] and img[tcy, tcx] == 0:
                    cv2.floodFill(img, mask, (tcx, tcy), 128)
                    area = np.sum(mask == 1)
                    if 500 < area < (img.shape[0] * img.shape[1] * 0.5):
                        filled = True
                        break
                    img[img == 128], mask[:] = 0, 0
            
            polygon_fill = 'class="room-area" pointer-events="all"'
            if filled:
                contours, _ = cv2.findContours(mask[1:-1, 1:-1], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                if contours:
                    c = max(contours, key=cv2.contourArea)
                    approx = cv2.approxPolyDP(c, 0.01 * cv2.arcLength(c, True), True)
                    points_str = " ".join([f"{pt[0][0]/scale:.1f},{pt[0][1]/scale:.1f}" for pt in approx])
                    polygon = f'<polygon points="{points_str}" {polygon_fill} />'
                    img[img == 128] = 0
                    return f'<a data-room="{room_id}" class="cursor-pointer group">\n{polygon}\n{modified_text}\n</a>'
            
            # Fallback to rect if floodfill fails
            rect = f'<rect x="{x-15}" y="{y-25}" width="60" height="40" rx="0" {polygon_fill} />'
            img[img == 128] = 0
            return f'<a data-room="{room_id}" class="cursor-pointer group">\n{rect}\n{modified_text}\n</a>'

        new_content = re.sub(r'<text id="([^"]+)"[^>]*>.*?</text>', process_match, content, flags=re.DOTALL)
        new_content = re.sub(r'<rect[^>]*fill="white"[^>]*/>', '', new_content)
        
        # Mark structural wall paths with class "plan-wall"
        def add_wall_class(match):
            tag = match.group(0)
            if 'class="' in tag:
                tag = re.sub(r'class="([^"]*)"', r'class="\1 plan-wall"', tag)
            else:
                tag = tag.replace('<path ', '<path class="plan-wall" ')
            # Replace inline stroke if present to allow CSS styling
            tag = re.sub(r'\sstroke=["\'][^"\']*["\']', '', tag)
            tag = re.sub(r'\sstroke-width=["\'][^"\']*["\']', '', tag)
            return tag

        new_content = re.sub(r'<path[^>]*>', add_wall_class, new_content)

        if "<style" not in new_content:
            new_content = re.sub(r'(<svg[^>]*?>)', r'\1\n' + EMBEDDED_STYLE, new_content, count=1)
        
        for out_dir in OUTPUT_DIRS:
            with open(os.path.join(out_dir, basename), 'w') as file: 
                file.write(new_content)

    # Also process foyer_map.csv if present
    process_foyer_map_csv(ROOT_DIR)

    # Copy PNG plan assets from input_pngs
    process_png_plans(ROOT_DIR)

    print("SVG & PNG Plan Processing complete.")

import shutil

def process_png_plans(root_dir=None):
    png_input_dir = str(SCRAPS_MANUAL_DIR / "input_pngs")
    if not os.path.exists(png_input_dir):
        return

    pngs = sorted(glob.glob(os.path.join(png_input_dir, "*.png")))
    if not pngs:
        return

    root_dir = str(BASE_DIR)
    out_dirs = [
        str(PLANS_DIR)
    ]

    for d in out_dirs:
        os.makedirs(d, exist_ok=True)

    copied_count = 0
    for src in pngs:
        basename = os.path.basename(src)
        aliases = [basename]
        if basename == "Foyer0.png":
            aliases.extend(["Foyer-0.png", "Foyer_0.png"])
        elif basename == "Foyer1.png":
            aliases.extend(["Foyer-1.png", "Foyer_1.png"])
        elif "-" in basename and not basename.startswith("U5-_-"):
            aliases.append(basename.replace("-", "_"))

        for out_dir in out_dirs:
            for alias in set(aliases):
                dest = os.path.join(out_dir, alias)
                shutil.copy2(src, dest)
        copied_count += 1

    print(f"  → Processed {copied_count} PNG plans from input_pngs to data/assets/plans")

if __name__ == "__main__":
    main()
