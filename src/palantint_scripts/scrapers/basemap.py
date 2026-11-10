"""Download the OpenStreetMap context around the campus as WGS84 GeoJSON.

The campus map draws roads, paths, buildings, land use and water under the
Wemap indoor plans. The raw Overpass response is archived in
``BASEMAP_ARCHIVE_DIR`` (rebuildable offline); the typed GeoJSON is published in
``GEO_PLANS_DIR`` and served only by the private map routes, so the map does not
depend on a third-party tile server at runtime.
Data © OpenStreetMap contributors, ODbL 1.0.
"""

import argparse
import asyncio
import json
import math
from datetime import datetime, timezone

import httpx

from palantint_scripts.config import BASEMAP_ARCHIVE_DIR, BASEMAP_PATH, GEO_PLANS_DIR
from palantint_scripts.scrapers.wemap import write_json

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "PalantINT campus map (contact: maintainers)"
# Used only when the Wemap extraction is not available yet.
FALLBACK_CENTER = (2.44378, 48.62527)
ATTRIBUTION = "© OpenStreetMap contributors"

ROAD_CLASSES = {
    "motorway": "major", "trunk": "major", "primary": "major",
    "motorway_link": "major", "trunk_link": "major", "primary_link": "major",
    "secondary": "secondary", "secondary_link": "secondary",
    "tertiary": "secondary", "tertiary_link": "secondary",
    "residential": "minor", "unclassified": "minor", "living_street": "minor",
    "service": "service", "track": "service",
    "pedestrian": "path", "footway": "path", "path": "path", "cycleway": "path",
    "steps": "path", "bridleway": "path", "corridor": "path",
}
KEPT_TAGS = (
    "name", "ref", "highway", "railway", "waterway", "building", "landuse", "leisure",
    "natural", "amenity", "service", "surface", "layer", "bridge", "tunnel",
    "building:levels", "height", "operator", "public_transport", "shop",
)


def campus_center():
    path = GEO_PLANS_DIR / "campus.geojson"
    try:
        bounds = json.loads(path.read_text(encoding="utf-8"))["metadata"]["sources"][0]["bounds"]
        return ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2)
    except (OSError, KeyError, IndexError, ValueError):
        return FALLBACK_CENTER


def bounds_around(center, radius_m):
    """WGS84 (west, south, east, north) of a square of half-side radius_m."""
    lon, lat = center
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def overpass_query(bounds):
    west, south, east, north = bounds
    bbox = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:180];
(
  way["highway"]({bbox});
  way["railway"]({bbox});
  way["waterway"]({bbox});
  way["building"]({bbox});
  relation["building"]["type"="multipolygon"]({bbox});
  way["landuse"]({bbox});
  relation["landuse"]["type"="multipolygon"]({bbox});
  way["leisure"]({bbox});
  relation["leisure"]["type"="multipolygon"]({bbox});
  way["natural"]({bbox});
  relation["natural"]["type"="multipolygon"]({bbox});
  way["amenity"="parking"]({bbox});
  node["railway"="station"]({bbox});
  node["public_transport"="station"]({bbox});
  node["highway"="bus_stop"]["name"]({bbox});
  node["amenity"]["name"]({bbox});
  node["shop"]["name"]({bbox});
);
out geom;"""


def feature_kind(tags, closed):
    if "building" in tags and tags.get("building") != "no":
        return "building"
    if "highway" in tags:
        if closed and tags.get("area") == "yes":
            return "pedestrian_area"
        return "road" if tags["highway"] in ROAD_CLASSES else None
    if "railway" in tags:
        return "railway" if tags["railway"] in ("rail", "light_rail", "tram", "subway", "station") else None
    if "waterway" in tags:
        return "waterway"
    if tags.get("natural") == "water" or tags.get("landuse") in ("reservoir", "basin") or "water" in tags:
        return "water"
    if tags.get("amenity") == "parking":
        return "parking"
    if tags.get("leisure") in ("park", "garden", "pitch", "playground", "sports_centre", "track", "stadium"):
        return "leisure"
    if tags.get("natural") in ("wood", "scrub", "grassland", "heath", "tree_row"):
        return "vegetation"
    if tags.get("landuse") in ("grass", "forest", "meadow", "recreation_ground", "village_green", "allotments", "cemetery", "orchard"):
        return "vegetation"
    if "landuse" in tags:
        return "landuse"
    return None


def properties(tags, kind, osm_id):
    props = {"kind": kind, "osm_id": osm_id}
    props.update({key.replace(":", "_"): tags[key] for key in KEPT_TAGS if key in tags})
    if kind == "road":
        props["class"] = ROAD_CLASSES[tags["highway"]]
    return props


def ring(points):
    return [[round(p["lon"], 7), round(p["lat"], 7)] for p in points]


def relation_polygons(element):
    """Assemble multipolygon member ways into rings; open fragments are dropped."""
    from shapely.geometry import LineString, mapping
    from shapely.ops import polygonize, unary_union

    outers, inners = [], []
    for member in element.get("members", []):
        if member.get("type") != "way" or len(member.get("geometry") or []) < 2:
            continue
        line = LineString([(p["lon"], p["lat"]) for p in member["geometry"]])
        (inners if member.get("role") == "inner" else outers).append(line)
    if not outers:
        return None
    shell = unary_union(list(polygonize(unary_union(outers))))
    if inners:
        shell = shell.difference(unary_union(list(polygonize(unary_union(inners)))))
    if shell.is_empty:
        return None
    return json.loads(json.dumps(mapping(shell)))


def to_features(elements):
    features = []
    for element in elements:
        tags = element.get("tags") or {}
        osm_id = f"{element['type']}/{element['id']}"
        if element["type"] == "node":
            if not tags.get("name"):
                continue
            kind = "station" if tags.get("railway") == "station" or tags.get("public_transport") == "station" else "bus_stop" if tags.get("highway") == "bus_stop" else "poi"
            geometry = {"type": "Point", "coordinates": [round(element["lon"], 7), round(element["lat"], 7)]}
            features.append({"type": "Feature", "id": osm_id, "geometry": geometry, "properties": properties(tags, kind, osm_id)})
            continue
        if element["type"] == "relation":
            kind = feature_kind(tags, True)
            geometry = relation_polygons(element) if kind else None
            if geometry:
                features.append({"type": "Feature", "id": osm_id, "geometry": geometry, "properties": properties(tags, kind, osm_id)})
            continue
        points = element.get("geometry") or []
        if len(points) < 2:
            continue
        closed = len(points) >= 4 and points[0] == points[-1]
        kind = feature_kind(tags, closed)
        if not kind:
            continue
        coordinates = ring(points)
        is_area = closed and kind not in ("road", "railway", "waterway")
        geometry = {"type": "Polygon", "coordinates": [coordinates]} if is_area else {"type": "LineString", "coordinates": coordinates}
        features.append({"type": "Feature", "id": osm_id, "geometry": geometry, "properties": properties(tags, kind, osm_id)})
    return features


def build_basemap(payload, bounds):
    """Typed GeoJSON from an archived Overpass response; no network access."""
    features = to_features(payload.get("elements", []))
    return {
        "type": "FeatureCollection",
        "metadata": {
            "schema_version": 1,
            "source": "openstreetmap",
            "attribution": ATTRIBUTION,
            "license": "ODbL-1.0",
            "osm_base": payload.get("osm3s", {}).get("timestamp_osm_base"),
            "extracted_at": payload.get("archived_at") or datetime.now(timezone.utc).isoformat(),
            "bounds": [round(value, 7) for value in bounds],
            "feature_count": len(features),
        },
        "features": features,
    }


async def scrape_basemap(progress=None, task_id=None, config=None, log=print):
    """Harvest the Overpass extract into the archive, then publish the GeoJSON.

    ``config["basemap_offline"]`` rebuilds from the archive without downloading.
    """
    config = config or {}
    radius = config.get("basemap_radius", 1500)
    archive = BASEMAP_ARCHIVE_DIR / "overpass.json"
    if config.get("basemap_offline"):
        if not archive.is_file():
            raise FileNotFoundError(f"Archive OpenStreetMap absente : {archive}")
        payload = json.loads(archive.read_text(encoding="utf-8"))
    else:
        log(f"Téléchargement du fond OpenStreetMap ({radius} m autour du campus)…")
        bounds = bounds_around(campus_center(), radius)
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=240) as client:
            response = await client.post(OVERPASS_URL, data={"data": overpass_query(bounds)})
        response.raise_for_status()
        payload = {**response.json(), "bounds": list(bounds), "archived_at": datetime.now(timezone.utc).isoformat()}
        write_json(archive, payload)
    collection = build_basemap(payload, payload["bounds"])
    write_json(BASEMAP_PATH, collection)
    log(f"Fond de carte : {len(collection['features'])} objets OpenStreetMap publiés")
    return collection


def main(argv=None):
    parser = argparse.ArgumentParser(description="Télécharger le fond OpenStreetMap autour du campus")
    parser.add_argument("--radius", type=int, default=1500, help="Demi-côté de l’emprise, en mètres")
    parser.add_argument("--offline", action="store_true", help="Reconstruire depuis la réponse archivée")
    args = parser.parse_args(argv)
    asyncio.run(scrape_basemap(config={"basemap_radius": args.radius, "basemap_offline": args.offline}))


if __name__ == "__main__":
    main()
