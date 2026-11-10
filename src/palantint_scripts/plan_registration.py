"""Place the historical floor plans on the geographic campus map.

The SVG-derived plans carry the authoritative room numbers in local drawing
coordinates. Wemap carries WGS84 geometry with a weaker numbering. For each
plan we estimate a similarity transform (rotation, uniform scale, translation)
from plan units to metres east/south of a fixed origin, using in order:

1. ``room_labels``: room numbers present in both sources, fitted with RANSAC;
2. ``room_shapes``: the plan's room outlines matched against the Wemap rooms of
   the same building, for buildings whose Wemap rooms are unnumbered;
3. ``outline``: the plan's footprint matched against the named building outline
   (Wemap, then OpenStreetMap).

A shape match must be unambiguous (no other orientation scoring nearly as well)
and close, otherwise the plan stays unplaced and the map keeps the Wemap rooms.
Manual control points (``plan-georeferencing.json``, applied by ``map_gen``)
always take precedence: those plans already carry WGS84 geometry.

The result is written to ``PLAN_REGISTRATION_PATH`` (harvest archive, private)
and ingested onto the plan FLOOR locations by
``palantint_scripts.loaders.plan_georeference``; only the private map routes
read it.
"""

import asyncio
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import cv2
import numpy as np

from palantint_scripts.config import BASEMAP_PATH, GEO_PLANS_DIR, PLAN_REGISTRATION_PATH, PLANS_DIR
from palantint_scripts.scrapers.wemap import write_json

RADIUS = 6378137
LABEL_TOLERANCE_M = 2.0
LABEL_MAX_RMS_M = 1.5
SHAPE_MAX_MEAN_M = 0.6
SHAPE_MIN_INLIERS = 0.6
SHAPE_AMBIGUITY = 0.8


def room_code(value):
    return re.sub(r"[^0-9A-Z]", "", str(value or "").upper())


class Frame:
    """Local tangent plane in metres: x east, y south (plan drawings are y-down)."""

    def __init__(self, origin):
        self.origin = origin
        self.k = math.pi / 180 * RADIUS

    def project(self, point):
        return (
            (point[0] - self.origin[0]) * self.k * math.cos(math.radians(self.origin[1])),
            -(point[1] - self.origin[1]) * self.k,
        )


def polygons_of(geometry):
    if not geometry:
        return []
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    return []


def fit_similarity(source, target):
    """Least-squares similarity mapping source points onto target points."""
    s, t = np.asarray(source, float), np.asarray(target, float)
    ms, mt = s.mean(0), t.mean(0)
    S, T = s - ms, t - mt
    spread = (S**2).sum()
    if spread < 1e-9:
        return None
    a = (S[:, 0] * T[:, 0] + S[:, 1] * T[:, 1]).sum() / spread
    b = (S[:, 0] * T[:, 1] - S[:, 1] * T[:, 0]).sum() / spread
    return np.array([a, b, mt[0] - (a * ms[0] - b * ms[1]), mt[1] - (b * ms[0] + a * ms[1])])


def apply(transform, points):
    a, b, tx, ty = transform
    p = np.asarray(points, float)
    return np.stack([a * p[:, 0] - b * p[:, 1] + tx, b * p[:, 0] + a * p[:, 1] + ty], axis=1)


def describe(transform):
    a, b, tx, ty = (float(value) for value in transform)
    return {"a": a, "b": b, "tx": tx, "ty": ty, "scale_m_per_unit": math.hypot(a, b), "rotation_deg": math.degrees(math.atan2(b, a))}


def fit_labels(pairs, seed=0):
    """RANSAC over (plan point, metric point, level) pairs."""
    if len(pairs) < 3:
        return None
    rng = random.Random(seed)
    best = []
    trials = 400 if len(pairs) > 3 else 1
    for _ in range(trials):
        sample = rng.sample(pairs, 3)
        transform = fit_similarity([p[0] for p in sample], [p[1] for p in sample])
        if transform is None:
            continue
        errors = np.linalg.norm(apply(transform, [p[0] for p in pairs]) - np.array([p[1] for p in pairs]), axis=1)
        inliers = [pair for pair, error in zip(pairs, errors) if error < LABEL_TOLERANCE_M]
        if len(inliers) > len(best):
            best = inliers
    if len(best) < max(3, min(4, len(pairs))):
        return None
    transform = fit_similarity([p[0] for p in best], [p[1] for p in best])
    errors = np.linalg.norm(apply(transform, [p[0] for p in best]) - np.array([p[1] for p in best]), axis=1)
    rms = float(np.sqrt(np.mean(errors**2)))
    if rms > LABEL_MAX_RMS_M:
        return None
    level = Counter(p[2] for p in best).most_common(1)[0][0]
    return {"method": "room_labels", "transform": transform, "anchors": len(best), "rms_m": rms, "level": level}


def sample_rings(rings, step):
    points = []
    for ring in rings:
        ring = np.asarray(ring, float)
        for start, end in zip(ring, np.roll(ring, -1, axis=0)):
            count = max(1, int(np.linalg.norm(end - start) / step))
            points.extend(start + (end - start) * t for t in np.arange(count) / count)
    return np.array(points)


class DistanceField:
    """Distance (metres) to the nearest target edge, from a rasterised boundary image."""

    def __init__(self, rings, resolution=0.2, margin=25):
        points = np.concatenate([np.asarray(ring, float) for ring in rings])
        self.origin = points.min(0) - margin
        self.resolution = resolution
        size = np.ceil((points.max(0) + margin - self.origin) / resolution).astype(int) + 1
        image = np.full((size[1], size[0]), 255, np.uint8)
        for ring in rings:
            pixels = np.round((np.asarray(ring, float) - self.origin) / resolution).astype(np.int32)
            cv2.polylines(image, [pixels], True, 0, 1)
        field, labels = cv2.distanceTransformWithLabels(image, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        self.field = field * resolution
        # Label i (1-based) is the i-th edge pixel in row-major order.
        rows, cols = np.nonzero(image == 0)
        self.edge_pixels = np.stack([cols, rows], axis=1) * resolution + self.origin
        self.labels = labels

    def lookup(self, points):
        pixels = np.round((points - self.origin) / self.resolution).astype(int)
        inside = (pixels[:, 0] >= 0) & (pixels[:, 1] >= 0) & (pixels[:, 0] < self.field.shape[1]) & (pixels[:, 1] < self.field.shape[0])
        return pixels, inside

    def distances(self, points, cap=3.0):
        pixels, inside = self.lookup(points)
        values = np.full(len(points), cap)
        values[inside] = np.minimum(self.field[pixels[inside, 1], pixels[inside, 0]], cap)
        return values

    def nearest(self, points):
        """Nearest target edge point, or NaN outside the field."""
        pixels, inside = self.lookup(points)
        result = np.full((len(points), 2), np.nan)
        result[inside] = self.edge_pixels[self.labels[pixels[inside, 1], pixels[inside, 0]] - 1]
        return result


def refine(transform, source, field, iterations=30):
    """Trimmed ICP towards the target edges."""
    for _ in range(iterations):
        moved = apply(transform, source)
        nearest = field.nearest(moved)
        errors = np.linalg.norm(moved - nearest, axis=1)
        valid = np.isfinite(errors)
        if not valid.any():
            break
        keep = valid & (errors < max(0.5, np.percentile(errors[valid], 70)))
        if keep.sum() < 10:
            break
        updated = fit_similarity(source[keep], nearest[keep])
        if updated is None or np.abs(updated - transform).max() < 1e-6:
            break
        transform = updated
    return transform


def match_shape(source_rings, target_rings, scales):
    """Search orientation and scale, refine, and reject ambiguous fits."""
    field = DistanceField(target_rings)
    source_scale = float(np.median(scales))
    source = sample_rings(source_rings, 0.35 / source_scale)
    if len(source) > 1500:
        source = source[np.linspace(0, len(source) - 1, 1500).astype(int)]
    target_center = field.edge_pixels.mean(0)
    source_center = source.mean(0)
    coarse = []
    for scale in scales:
        for degrees in range(0, 360, 3):
            angle = math.radians(degrees)
            a, b = scale * math.cos(angle), scale * math.sin(angle)
            tx = target_center[0] - (a * source_center[0] - b * source_center[1])
            ty = target_center[1] - (b * source_center[0] + a * source_center[1])
            transform = np.array([a, b, tx, ty])
            coarse.append((field.distances(apply(transform, source)).mean(), degrees, transform))
    coarse.sort(key=lambda item: item[0])
    refined = []
    for _, degrees, transform in coarse[:12]:
        transform = refine(transform, source, field)
        distances = field.distances(apply(transform, source))
        refined.append((float(distances.mean()), float((distances < 0.5).mean()), transform))
    refined.sort(key=lambda item: item[0])
    best = refined[0]
    rotation = math.degrees(math.atan2(best[2][1], best[2][0]))
    rivals = [item for item in refined[1:] if abs((math.degrees(math.atan2(item[2][1], item[2][0])) - rotation + 180) % 360 - 180) > 15]
    ambiguity = best[0] / rivals[0][0] if rivals and rivals[0][0] > 0 else 0.0
    return {"transform": best[2], "mean_m": best[0], "inliers": best[1], "ambiguity": ambiguity}


def footprint_rings(rings, scale):
    """Outer contour of the plan's rooms, closing gaps of about 1.5 m between rooms."""
    points = np.concatenate([np.asarray(ring, float) for ring in rings])
    resolution = 0.25 / scale
    origin = points.min(0) - 4 / scale
    size = np.ceil((points.max(0) + 4 / scale - origin) / resolution).astype(int) + 1
    image = np.zeros((size[1], size[0]), np.uint8)
    for ring in rings:
        cv2.fillPoly(image, [np.round((np.asarray(ring) - origin) / resolution).astype(np.int32)], 255)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    image = cv2.morphologyEx(image, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    return [contour.reshape(-1, 2) * resolution + origin]


def ring_area(rings):
    total = 0.0
    for ring in rings:
        r = np.asarray(ring, float)
        total += abs(np.dot(r[:, 0], np.roll(r[:, 1], -1)) - np.dot(r[:, 1], np.roll(r[:, 0], -1))) / 2
    return total


def residence_buildings(collection):
    """Wemap building ids by residence code, from the building names (``U1 U2 U4``)."""
    names = {}
    for feature in collection["features"]:
        props = feature["properties"]
        if props.get("kind") == "building" and props.get("name"):
            for token in re.split(r"[\s,]+", props["name"].upper()):
                if token:
                    names.setdefault(token, []).append(props["building_id"])
    return names


def load_plans():
    plans = []
    for path in sorted(PLANS_DIR.glob("*.geojson")):
        plan = json.loads(path.read_text(encoding="utf-8"))
        if plan.get("metadata", {}).get("plan_coordinate_space") == "local_plan":
            plans.append((path.stem, plan))
    return plans


def plan_rooms(plan):
    rooms = []
    for feature in plan["features"]:
        props = feature["properties"]
        if props.get("kind") == "room" and props.get("plan_geometry"):
            rooms.append((props, polygons_of(props["plan_geometry"])))
    return rooms


def register_plans(log=print):
    collection = json.loads((GEO_PLANS_DIR / "campus.geojson").read_text(encoding="utf-8"))
    bounds = collection["metadata"]["sources"][0]["bounds"]
    frame = Frame([round((bounds[0] + bounds[2]) / 2, 6), round((bounds[1] + bounds[3]) / 2, 6)])
    by_code = residence_buildings(collection)
    wemap_names = {f["properties"]["building_id"]: f["properties"].get("name") for f in collection["features"] if f["properties"].get("kind") == "building"}

    labels = defaultdict(list)
    rooms_by_level = defaultdict(list)
    outlines = {}
    for feature in collection["features"]:
        props = feature["properties"]
        building = props.get("building_id")
        if props.get("kind") == "room":
            rings = [[frame.project(p) for p in poly[0]] for poly in polygons_of(feature["geometry"])]
            for level in props.get("levels") or []:
                rooms_by_level[(building, level)].extend(rings)
            if props.get("name") and props.get("label_coordinates"):
                labels[room_code(props["name"])].append((building, (props.get("levels") or [None])[0], frame.project(props["label_coordinates"])))
        elif props.get("kind") == "building":
            outlines[building] = [[frame.project(p) for p in poly[0]] for poly in polygons_of(feature["geometry"])]
    osm_outlines = {}
    if BASEMAP_PATH.exists():
        for feature in json.loads(BASEMAP_PATH.read_text(encoding="utf-8"))["features"]:
            props = feature["properties"]
            if props.get("kind") == "building" and props.get("name"):
                osm_outlines.setdefault(props["name"].upper(), [[frame.project(p) for p in poly[0]] for poly in polygons_of(feature["geometry"])])

    results, unplaced, plans = {}, {}, load_plans()
    for name, plan in plans:
        meta = plan["metadata"]
        code = meta["building"].upper()
        if meta.get("georeferenced"):
            results[name] = {"building": meta["building"], "floor": meta["floor"], "method": "control_points"}
            continue
        candidates = set(by_code.get(code, []))
        pairs = []
        for props, _ in plan_rooms(plan):
            point = props.get("label_coordinates")
            matches = [m for m in labels.get(room_code(props.get("room_id") or props.get("name")), []) if m[0] in candidates]
            if point and len(matches) == 1:
                pairs.append((point, matches[0][2], matches[0][1]))
        fit = fit_labels(pairs)
        if fit:
            fit["wemap_building_id"] = next(iter(candidates)) if len(candidates) == 1 else None
            results[name] = fit
        else:
            results[name] = None
        results[name] = results[name] or {"pairs": len(pairs)}
        results[name].update({"building": meta["building"], "floor": meta["floor"]})

    # Scale prior: drawings of one campus share similar units; use fitted plans.
    fitted_scales = [math.hypot(*r["transform"][:2]) for r in results.values() if "transform" in r]
    prior = float(np.median(fitted_scales)) if fitted_scales else 0.05

    for name, plan in plans:
        result = results[name]
        if "transform" in result or result.get("method") == "control_points":
            continue
        meta = plan["metadata"]
        code = meta["building"].upper()
        rooms = plan_rooms(plan)
        source_rings = [poly[0] for _, polys in rooms for poly in polys]
        if len(source_rings) < 3:
            unplaced[name] = "Pas assez de salles dans le plan."
            continue
        source_area = ring_area(source_rings)
        attempts = []
        for building in by_code.get(code, []):
            levels = sorted({level for (b, level) in rooms_by_level if b == building}, key=lambda level: abs(level - float(meta["floor"])))
            for level in levels[:3]:
                target = rooms_by_level[(building, level)]
                if len(target) < 3:
                    continue
                base = math.sqrt(ring_area(target) / source_area)
                scales = [base * f for f in (0.85, 0.92, 1.0, 1.08, 1.16)] + [prior]
                attempts.append(("room_shapes", building, level, match_shape(source_rings, target, scales)))
        if not attempts:
            outline = None
            for building in by_code.get(code, []):
                outline = outline or outlines.get(building)
            outline = outline or osm_outlines.get(code)
            footprint = footprint_rings(source_rings, prior)
            if outline and footprint:
                base = math.sqrt(ring_area(outline) / max(ring_area(footprint), 1e-9))
                scales = [base * f for f in (0.9, 0.95, 1.0, 1.05, 1.1)]
                attempts.append(("outline", (by_code.get(code) or [None])[0], None, match_shape(footprint, outline, scales)))
        if not attempts:
            unplaced[name] = "Aucun bâtiment géographique correspondant."
            continue
        method, building, level, match = min(attempts, key=lambda item: item[3]["mean_m"])
        accepted = match["mean_m"] <= SHAPE_MAX_MEAN_M and match["inliers"] >= SHAPE_MIN_INLIERS and match["ambiguity"] <= SHAPE_AMBIGUITY
        details = f"écart moyen {match['mean_m']:.2f} m, {match['inliers']:.0%} de contours ajustés, ambiguïté {match['ambiguity']:.2f}"
        if not accepted:
            unplaced[name] = f"Correspondance de forme insuffisante ({details})."
            continue
        result.update({"method": method, "transform": match["transform"], "mean_distance_m": match["mean_m"], "inlier_ratio": match["inliers"], "ambiguity": match["ambiguity"], "wemap_building_id": building})
        if level is not None:
            result["level"] = level

    placed = {}
    for name, result in results.items():
        if "transform" not in result and result.get("method") != "control_points":
            unplaced.setdefault(name, f"Pas assez de numéros de salle communs ({result.get('pairs', 0)}).")
            continue
        entry = {key: value for key, value in result.items() if key not in ("transform", "pairs")}
        if "transform" in result:
            entry["transform"] = describe(result["transform"])
        if entry.get("wemap_building_id") is not None:
            entry["wemap_building_name"] = wemap_names.get(entry["wemap_building_id"])
        placed[name] = entry

    # Floors stack consistently within a building. A floor without its own Wemap
    # level, or whose shape matched a lower storey with the same layout, takes
    # the level offset shared by the other floors of its building.
    offsets = defaultdict(list)
    for entry in placed.values():
        if entry.get("level") is not None:
            offsets[entry["building"]].append(entry["level"] - float(entry["floor"]))
    for entry in placed.values():
        counted = Counter(offsets.get(entry["building"], [])).most_common(1)
        if not counted or counted[0][1] < 2 and entry.get("level") is not None:
            continue
        expected = float(entry["floor"]) + counted[0][0]
        if entry.get("level") != expected and (entry.get("level") is None or entry["method"] != "room_labels"):
            entry["level"] = expected
            entry["level_source"] = "building_offset"

    output = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "frame": {"origin": frame.origin, "axes": "east_south_metres", "earth_radius": RADIUS},
        "sources": {"wemap_extracted_at": collection["metadata"].get("extracted_at")},
        "plans": placed,
        "unplaced": unplaced,
    }
    write_json(PLAN_REGISTRATION_PATH, output)
    for name, entry in sorted(placed.items()):
        quality = f"{entry['anchors']} repères, RMS {entry['rms_m']:.2f} m" if entry["method"] == "room_labels" else f"écart moyen {entry.get('mean_distance_m', 0):.2f} m"
        log(f"  {name:10} {entry['method']:14} niveau {entry.get('level')}  {quality}")
    for name, reason in sorted(unplaced.items()):
        log(f"  {name:10} non placé : {reason}")
    log(f"{len(placed)} plans placés, {len(unplaced)} non placés → {PLAN_REGISTRATION_PATH}")
    return output


async def scrape_plan_registration(progress=None, task_id=None, config=None, log=print):
    """Pipeline step: derive the placements from the local Wemap and plan files."""
    return await asyncio.to_thread(register_plans, log)


def main():
    register_plans()


if __name__ == "__main__":
    main()
