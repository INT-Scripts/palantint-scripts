"""Convert archival SVG drawings into plan GeoJSON, never generated SVG UI.

RFC 7946 geometry is null until geographic control points are supplied. Original
plan geometry lives in properties.plan_geometry in an explicitly declared local
frame; consumers must never interpret these coordinates as longitude/latitude.
"""

import csv
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from svgpathtools import parse_path

from palantint_scripts.config import ASSETS_DIR, PLANS_DIR, SCRAPS_MANUAL_DIR


def matrix(value):
    result = np.eye(3)
    for name, numbers in re.findall(r"([A-Za-z]+)\s*\(([^)]*)\)", value or ""):
        v = [
            float(n)
            for n in re.findall(r"[-+]?(?:\d*\.)?\d+(?:[eE][-+]?\d+)?", numbers)
        ]
        step = np.eye(3)
        if name == "matrix" and len(v) == 6:
            step = np.array([[v[0], v[2], v[4]], [v[1], v[3], v[5]], [0, 0, 1]])
        elif name == "translate":
            step[0, 2], step[1, 2] = v[0], v[1] if len(v) > 1 else 0
        elif name == "scale":
            step[0, 0], step[1, 1] = v[0], v[1] if len(v) > 1 else v[0]
        elif name == "rotate":
            angle = math.radians(v[0])
            c, s = math.cos(angle), math.sin(angle)
            step = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
            if len(v) == 3:
                step = (
                    matrix(f"translate({v[1]} {v[2]})")
                    @ step
                    @ matrix(f"translate({-v[1]} {-v[2]})")
                )
        else:
            raise ValueError(f"Transformation SVG non prise en charge : {name}")
        result = result @ step
    return result


def convert_svg(path, raster_scale=2, control_points=None):
    path = Path(path)
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    view = [float(n) for n in root.get("viewBox", "").replace(",", " ").split()]
    if not view:

        def number(value):
            return float(re.match(r"[\d.]+", value)[0])

        view = [
            0,
            0,
            number(root.get("width", "1000")),
            number(root.get("height", "1000")),
        ]
    if len(view) != 4 or view[2] <= 0 or view[3] <= 0:
        raise ValueError(f"Dimensions de plan invalides : {path.name}")
    building, floor = path.stem.rsplit("_", 1)
    origin_x, origin_y, width, height = view
    if width * height * raster_scale**2 > 40_000_000:
        raise ValueError(f"Plan trop grand pour une extraction sûre : {path.name}")
    walls = []
    labels = []

    def point(x, y, transform):
        p = transform @ np.array([float(x), float(y), 1])
        return [round(float(p[0]), 4), round(float(p[1]), 4)]

    def walk(element, parent_transform):
        transform = parent_transform @ matrix(element.get("transform"))
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "path":
            parsed = parse_path(element.get("d", ""))
            for subpath in parsed.continuous_subpaths():
                coords = []
                for segment in subpath:
                    count = max(1, min(4096, math.ceil(segment.length(error=1e-4) / 2)))
                    if segment.__class__.__name__ == "Line":
                        count = 1
                    for t in np.linspace(0, 1, count + 1):
                        p = segment.point(float(t))
                        candidate = point(p.real, p.imag, transform)
                        if not coords or coords[-1] != candidate:
                            coords.append(candidate)
                if len(coords) > 1:
                    walls.append(coords)
        elif tag in ("line", "polyline", "polygon", "rect"):
            if tag == "line":
                coords = [
                    [element.get("x1", 0), element.get("y1", 0)],
                    [element.get("x2", 0), element.get("y2", 0)],
                ]
            elif tag == "rect":
                x, y, w, h = [
                    float(element.get(k, 0)) for k in ("x", "y", "width", "height")
                ]
                coords = [[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]
            else:
                numbers = [
                    float(n)
                    for n in re.findall(
                        r"[-+]?(?:\d*\.)?\d+", element.get("points", "")
                    )
                ]
                coords = list(zip(numbers[::2], numbers[1::2]))
                if tag == "polygon" and coords:
                    coords.append(coords[0])
            if len(coords) > 1:
                walls.append([point(x, y, transform) for x, y in coords])
        elif tag == "text" and element.get("id"):
            positioned = (
                element
                if element.get("x") is not None
                else next(
                    (child for child in element if child.get("x") is not None), None
                )
            )
            if positioned is None or positioned.get("y") is None:
                raise ValueError(
                    f"Étiquette sans coordonnées : {path.name}/{element.get('id')}"
                )
            local_transform = (
                transform
                if positioned is element
                else transform @ matrix(positioned.get("transform"))
            )
            label = point(
                positioned.get("x").split()[0],
                positioned.get("y").split()[0],
                local_transform,
            )
            labels.append(
                (element.get("id"), label, "".join(element.itertext()).strip())
            )
        elif tag not in ("svg", "g", "text", "tspan", "defs", "style", "title", "desc"):
            raise ValueError(f"Élément SVG à convertir explicitement : {tag}")
        for child in element:
            walk(child, transform)

    walk(root, np.eye(3))
    if len({identity for identity, _, _ in labels}) != len(labels):
        raise ValueError(f"Identifiants de salles dupliqués : {path.name}")
    raster = np.zeros(
        (math.ceil(height * raster_scale) + 4, math.ceil(width * raster_scale) + 4),
        dtype=np.uint8,
    )

    def to_pixel(p):
        return (
            round((p[0] - origin_x) * raster_scale) + 2,
            round((p[1] - origin_y) * raster_scale) + 2,
        )

    for coords in walls:
        cv2.polylines(
            raster,
            [np.array([to_pixel(p) for p in coords], dtype=np.int32)],
            False,
            255,
            max(1, round(raster_scale)),
        )
    _, regions, stats, _ = cv2.connectedComponentsWithStats(
        255 - raster, connectivity=4
    )
    extracted = []
    for identity, label, text in labels:
        x, y = to_pixel([label[0], label[1] - 4])
        region = 0
        for dx, dy in ((0, 0), (0, -3), (0, 3), (3, 0), (-3, 0)):
            px, py = x + dx, y + dy
            if (
                0 <= px < raster.shape[1]
                and 0 <= py < raster.shape[0]
                and regions[py, px]
            ):
                candidate = int(regions[py, px])
                left, top, w, h, area = stats[candidate]
                if (
                    left > 0
                    and top > 0
                    and left + w < raster.shape[1]
                    and top + h < raster.shape[0]
                    and 20 < area
                ):
                    region = candidate
                    break
        extracted.append((identity, label, text, region))
    counts = Counter(item[3] for item in extracted if item[3])
    features = []

    def feature(identity, kind, geometry, **properties):
        return {
            "type": "Feature",
            "id": f"plan:{building}:{floor}:{kind}:{identity}",
            "geometry": None,
            "properties": {
                "kind": kind,
                "building": building,
                "floor": floor,
                "plan_geometry": geometry,
                **properties,
            },
        }

    for i, coords in enumerate(walls):
        features.append(
            feature(str(i), "wall", {"type": "LineString", "coordinates": coords})
        )
    missing = []
    for identity, label, text, region in extracted:
        geometry = {"type": "Point", "coordinates": label}
        status = "unresolved"
        if region and counts[region] == 1:
            contours, hierarchy = cv2.findContours(
                np.uint8(regions == region) * 255,
                cv2.RETR_CCOMP,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            polygons = []

            def ring(contour):
                points = cv2.approxPolyDP(contour, 0.4 * raster_scale, True).reshape(
                    -1, 2
                )
                output = [
                    [
                        round((float(x) - 2) / raster_scale + origin_x, 3),
                        round((float(y) - 2) / raster_scale + origin_y, 3),
                    ]
                    for x, y in points
                ]
                if len(output) >= 3:
                    output.append(output[0])
                    return output
                return None

            if hierarchy is not None:
                for i, contour in enumerate(contours):
                    if hierarchy[0][i][3] != -1:
                        continue
                    outer = ring(contour)
                    if not outer:
                        continue
                    rings = [outer]
                    child = hierarchy[0][i][2]
                    while child != -1:
                        hole = ring(contours[child])
                        if hole:
                            rings.append(hole)
                        child = hierarchy[0][child][0]
                    polygons.append(rings)
            if polygons:
                geometry = (
                    {"type": "Polygon", "coordinates": polygons[0]}
                    if len(polygons) == 1
                    else {"type": "MultiPolygon", "coordinates": polygons}
                )
                status = "wall_enclosure"
        if status == "unresolved":
            missing.append(identity)
        features.append(
            feature(
                identity,
                "room",
                geometry,
                room_id=identity,
                name=text or identity,
                label_coordinates=label,
                extraction=status,
            )
        )
    georeference = None
    if control_points:
        if len(control_points) < 3:
            raise ValueError("Au moins trois repères géographiques sont nécessaires")
        origin = control_points[0]["geographic"]
        if not all(
            len(p["plan"]) == 2
            and all(math.isfinite(float(v)) for v in p["plan"])
            and len(p["geographic"]) == 2
            and -180 <= p["geographic"][0] <= 180
            and -90 < p["geographic"][1] < 90
            for p in control_points
        ):
            raise ValueError("Repères géographiques invalides")
        from services.map_registration import RADIUS, project

        source = np.array([[*p["plan"], 1] for p in control_points], dtype=float)
        target = np.array([project(p["geographic"], origin) for p in control_points])
        coefficients, _, rank, _ = np.linalg.lstsq(source, target, rcond=None)
        if rank < 3:
            raise ValueError("Les repères du plan sont alignés")
        if np.linalg.matrix_rank(coefficients[:2]) < 2:
            raise ValueError("Les repères géographiques sont alignés")
        residual = float(
            np.sqrt(np.mean(np.sum((source @ coefficients - target) ** 2, axis=1)))
        )
        if not math.isfinite(residual) or residual > 3:
            raise ValueError(
                "Les repères géographiques ne concordent pas (erreur supérieure à 3 m)"
            )

        def geographic(point):
            east, south = np.array([point[0], point[1], 1]) @ coefficients
            return [
                float(
                    origin[0]
                    + east
                    / (RADIUS * math.cos(math.radians(origin[1])))
                    * 180
                    / math.pi
                ),
                float(origin[1] - south / RADIUS * 180 / math.pi),
            ]

        def project_coordinates(coords):
            return (
                geographic(coords)
                if isinstance(coords[0], (int, float))
                else [project_coordinates(value) for value in coords]
            )

        for item in features:
            local = item["properties"]["plan_geometry"]
            item["geometry"] = {
                "type": local["type"],
                "coordinates": project_coordinates(local["coordinates"]),
            }
        georeference = {
            "control_points": control_points,
            "rms_error_m": residual,
            "method": "affine_control_points",
        }
    return {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "building": building,
            "floor": floor,
            "plan_coordinate_space": "local_plan",
            "view_box": view,
            "georeferenced": georeference is not None,
            "georeference": georeference,
            "source": path.name,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "room_count": len(labels),
            "unresolved_rooms": missing,
            "extraction_tolerance": 1 / raster_scale,
        },
        "features": features,
    }


def process_foyer_map_csv(root_dir=None):
    source = SCRAPS_MANUAL_DIR / "foyer_map.csv"
    if not source.is_file():
        return
    entries = {}
    with source.open(encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter=";"):
            identity = row.get("room_id", "").strip()
            if identity:
                entries[identity] = {
                    "room_id": identity,
                    "raw_name": row.get("club_name", "").strip(),
                    "club_name": row.get("club_name", "").strip(),
                    "floor": "1" if identity.startswith("F1") else "0",
                    "building": "Foyer",
                }
    destination = ASSETS_DIR / "clubs"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "foyer_map.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (destination / "foyer_map.csv").write_bytes(source.read_bytes())


def main():
    files = sorted((SCRAPS_MANUAL_DIR / "input_svgs").glob("*.svg"))
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    report = []
    for path in files:
        # Public plan files stay in local coordinates: manual control points are
        # applied privately by plan_registration.
        collection = convert_svg(path)
        target = PLANS_DIR / f"{path.stem}.geojson"
        temporary = target.with_suffix(".geojson.tmp")
        temporary.write_text(
            json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)
        report.append(collection["metadata"])
        print(
            f"{path.stem} : {collection['metadata']['room_count']} salles, {len(collection['metadata']['unresolved_rooms'])} contours à vérifier"
        )
    (PLANS_DIR / "manifest.json").write_text(
        json.dumps({"plans": report}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    process_foyer_map_csv()
    print(
        f"Conversion terminée : {len(report)} plans GeoJSON. Les sources SVG sont conservées."
    )


if __name__ == "__main__":
    main()
