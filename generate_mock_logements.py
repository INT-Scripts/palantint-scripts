"""Extract all room numbers (data-room attributes) from SVG plan files, then
generate mock apartment details for ALL rooms (not just occupied ones)."""
import json, os, re

PLANS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/assets/plans"))
EXPORTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/exports"))
SCRAP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/scrap"))

# Extract data-room from all SVGs
all_rooms = set()
for svg_file in os.listdir(PLANS_DIR):
    if not svg_file.endswith(".svg"):
        continue
    with open(os.path.join(PLANS_DIR, svg_file), encoding="utf-8") as f:
        content = f.read()
    rooms = re.findall(r'data-room="(\d+)"', content)
    all_rooms.update(rooms)

# Also add from apartments.json mapping
mapping_path = os.path.join(EXPORTS_DIR, "apartments.json")
if os.path.exists(mapping_path):
    with open(mapping_path, encoding="utf-8") as f:
        mapping = json.load(f)
    for r in mapping.values():
        r_str = str(r)
        if r_str.isdigit() and len(r_str) == 4:
            all_rooms.add(r_str)

print(f"Found {len(all_rooms)} unique rooms total (SVG + mapping)")

# Generate mock details
logements = {}
for r_str in sorted(all_rooms):
    if not r_str.isdigit() or len(r_str) < 4:
        continue
    b = r_str[0]
    f = r_str[1]
    room_num = int(r_str)
    is_double = (room_num % 10 == 0)

    if is_double:
        logements[r_str] = {
            "Bâtiment": f"U{b}", "Etage": f,
            "Type": "STUDIO DOUBLE", "Superficie": "35 m²",
            "Tarif": "620.00 €", "Allocation boursier": "220.00 €",
            "Allocation non boursier": "165.00 €",
            "_req_b": int(b), "_req_e": f
        }
    else:
        logements[r_str] = {
            "Bâtiment": f"U{b}", "Etage": f,
            "Type": "STUDIO INDIVIDUEL", "Superficie": "18 m²",
            "Tarif": "415.00 €", "Allocation boursier": "150.00 €",
            "Allocation non boursier": "110.00 €",
            "_req_b": int(b), "_req_e": f
        }

out_path = os.path.join(SCRAP_DIR, "logements.json")
os.makedirs(SCRAP_DIR, exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(logements, f, ensure_ascii=False, indent=4)

print(f"Wrote {len(logements)} room details to {out_path}")
