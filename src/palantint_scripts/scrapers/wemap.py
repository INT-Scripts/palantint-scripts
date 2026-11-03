"""Download Wemap indoor geometry and POIs as a self-contained WGS84 GeoJSON.

Pinpoint exports contain centres only. The style's indoor vector source contains
the actual floor plans. We download every tile at the source's native maximum
zoom, stitch fragments by source identity, and retain every source property.
No database or CAS access is needed for this harvesting phase.
"""

import argparse
import asyncio
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

from palantint_scripts.config import GEO_PLANS_DIR, WEMAP_DIR

API_URL = "https://api.getwemap.com/v3.0"
DEFAULT_MAP_ID = 29608


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


def tile_coordinates(bounds, zoom):
    """All XYZ tiles intersecting WGS84 bounds, inclusive of the boundary."""
    west, south, east, north = bounds
    if not (-180 <= west < east <= 180 and -85.051129 <= south < north <= 85.051129):
        raise ValueError("Emprise géographique Wemap invalide")

    def coordinate(lon, lat):
        return (
            math.floor((lon + 180) / 360 * 2**zoom),
            math.floor(
                (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**zoom
            ),
        )

    min_x, min_y = coordinate(west, north)
    max_x, max_y = coordinate(east, south)
    return [
        (zoom, x, y) for x in range(min_x, max_x + 1) for y in range(min_y, max_y + 1)
    ]


def feature_levels(properties):
    """Preserve fractional floors and explicit multi-floor ranges."""
    value = properties.get("level")
    if value is not None:
        try:
            return sorted(set(float(level.strip()) for level in str(value).split(";")))
        except ValueError:
            pass
    low, high = properties.get("min_level"), properties.get("max_level")
    if low is not None and high is not None:
        low, high = float(low), float(high)
        if low == high:
            return [low]
        if low.is_integer() and high.is_integer() and 0 <= high - low <= 100:
            return list(range(int(low), int(high) + 1))
        return sorted(set([low, high]))
    return []


def feature_kind(properties, geometry_type):
    if properties.get("type") == "building":
        return "building"
    if properties.get("indoor") == "level":
        return "floor"
    if properties.get("indoor") in ("room", "stand") and "Polygon" in geometry_type:
        return "room"
    if properties.get("highway") == "elevator":
        return "elevator"
    if properties.get("highway") == "steps":
        return "stairs"
    if properties.get("indoor") == "corridor":
        return "corridor"
    return "poi" if geometry_type == "Point" else "structure"


def decode_tiles(tiles, zoom):
    """Union fragments in the shared integer tile grid before reprojection.

    Buffered MVT tiles overlap. Concatenating their GeoJSON would duplicate
    rooms and leave seams. Source PK + representation + geometry dimension
    distinguishes a room polygon from its separate label and POI point.
    """
    import mapbox_vector_tile
    from shapely import make_valid
    from shapely.geometry import mapping, shape
    from shapely.ops import transform, unary_union

    fragments = defaultdict(list)
    metadata = {}
    for (z, x, y), payload in tiles:
        if not payload:
            continue
        decoded = mapbox_vector_tile.decode(
            payload, default_options={"y_coord_down": True}
        )
        for layer_name, layer in decoded.items():
            extent = layer["extent"]
            for feature in layer["features"]:
                props = feature.get("properties", {})
                geometry = feature["geometry"]
                family = geometry["type"].removeprefix("Multi")
                source_pk = props.get("pk", props.get("id", feature.get("id")))
                if source_pk is None:
                    # Geometry is part of identity for unkeyed source features.
                    source_pk = hashlib.sha256(
                        json.dumps(feature, sort_keys=True).encode()
                    ).hexdigest()[:24]
                identity = (
                    f"{layer_name}:{props.get('type', 'feature')}:{source_pk}:{family}"
                )
                geom = shape(geometry)
                if geom.is_empty:
                    continue
                if not geom.is_valid:
                    geom = make_valid(geom)
                global_geom = transform(
                    lambda px, py, pz=None: ((x + px / extent), (y + py / extent)), geom
                )
                fragments[identity].append(global_geom)
                metadata[identity] = dict(props)

    scale = 2**zoom

    def to_wgs84(x, y, z=None):
        return (
            x / scale * 360 - 180,
            math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale)))),
        )

    features = []
    for identity in sorted(fragments):
        geom = unary_union(fragments[identity])
        # Keep the complete geometry, including polygons' interior rings.
        geojson = mapping(transform(to_wgs84, geom))
        props = metadata[identity]
        props.update(
            {
                "source": "wemap",
                "source_feature_id": identity,
                "levels": feature_levels(props),
            }
        )
        props["kind"] = feature_kind(props, geojson["type"])
        features.append(
            {
                "type": "Feature",
                "id": identity,
                "geometry": geojson,
                "properties": props,
            }
        )
    return features


def enrich_features(features, pinpoints, livemap):
    """Join POIs by Wemap ID, never by a guessed nearest-room match."""
    from shapely.geometry import shape
    from shapely.ops import transform

    pois = {str(point["id"]): point for point in pinpoints}
    tags = {
        tag["slug"]: tag["name"]
        for tag in livemap.get("snippet", {}).get("usetags", {}).get("tags", [])
    }
    names = {}
    for feature in features:
        p = feature["properties"]
        if p.get("name") and p.get("pk") is not None:
            names[str(p["pk"])] = p["name"]
    matched = set()
    for feature in features:
        p = feature["properties"]
        pinpoint_id = str(p.get("pinpoint", p.get("wemap_id", "")))
        poi = pois.get(pinpoint_id)
        if poi:
            matched.add(pinpoint_id)
            p["poi"] = poi
            p["name"] = poi.get("name") or p.get("name")
            for key in (
                "description",
                "tags",
                "address",
                "opening_hours",
                "phone",
                "link_url",
                "image_url",
            ):
                if poi.get(key) is not None:
                    p[key] = poi[key]
        elif not p.get("name") and str(p.get("pk")) in names:
            p["name"] = names[str(p["pk"])]
        p["livemap_id"] = livemap["id"]
    # Retain POIs outside the indoor tiles / missing from the vector source.
    for poi_id, poi in pois.items():
        if (
            poi_id in matched
            or poi.get("latitude") is None
            or poi.get("longitude") is None
        ):
            continue
        props = dict(poi)
        props.update(
            {
                "source": "wemap",
                "source_feature_id": f"pinpoint:{poi_id}",
                "kind": "poi",
                "livemap_id": livemap["id"],
                "levels": feature_levels(poi),
            }
        )
        features.append(
            {
                "type": "Feature",
                "id": f"pinpoint:{poi_id}",
                "geometry": {
                    "type": "Point",
                    "coordinates": [poi["longitude"], poi["latitude"]],
                },
                "properties": props,
            }
        )
    for feature in features:
        props = feature["properties"]
        geom = shape(feature["geometry"])
        point = geom.representative_point()
        props["label_coordinates"] = [point.x, point.y]
        props["tag_names"] = [tags.get(tag, tag) for tag in props.get("tags", [])]
        feature["bbox"] = list(geom.bounds)
        if "Polygon" in geom.geom_type:
            latitude = point.y
            projected = transform(
                lambda x, y, z=None: (
                    x * 111320 * math.cos(math.radians(latitude)),
                    y * 111320,
                ),
                geom,
            )
            props["geometry_area_m2"] = round(projected.area, 2)
    return features


class WemapExtractor:
    def __init__(self, client, directory=WEMAP_DIR, concurrency=5, offline=False):
        self.client = client
        self.directory = Path(directory)
        self.semaphore = asyncio.Semaphore(max(1, min(concurrency, 16)))
        self.offline = offline

    async def download(self, url, relative_path, allow_missing=False):
        path = self.directory / relative_path
        if self.offline:
            return path.read_bytes()
        async with self.semaphore:
            for attempt in range(4):
                try:
                    response = await self.client.get(url)
                    if allow_missing and response.status_code == 404:
                        payload = (
                            b""  # Empty tiles outside the actual indoor footprint.
                        )
                    else:
                        response.raise_for_status()
                        payload = response.content
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(path.suffix + ".tmp")
                    temporary.write_bytes(payload)
                    temporary.replace(path)
                    return payload
                except (httpx.HTTPError, OSError):
                    if attempt == 3:
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)

    async def json(self, url, relative_path):
        return json.loads(await self.download(url, relative_path))

    async def paginated_pinpoints(self, map_id):
        points = {}
        offset = 0
        expected_count = None
        while True:
            url = (
                f"{API_URL}/pinpoints/search?livemap={map_id}&limit=100&offset={offset}"
            )
            page = await self.json(url, f"raw/pinpoints-{offset}.json")
            if page.get("count") is not None:
                if expected_count is not None and expected_count != page["count"]:
                    raise ValueError(
                        "La liste Wemap a changé pendant l’extraction ; recommencer pour obtenir un instantané complet"
                    )
                expected_count = page["count"]
            batch = page.get("results", [])
            if not batch:
                if offset < page.get("count", offset):
                    raise ValueError(
                        "Pagination Wemap interrompue avant la fin des résultats"
                    )
                break
            before = len(points)
            points.update((point["id"], point) for point in batch)
            if len(points) == before:
                raise ValueError("Pagination Wemap répétée : extraction incomplète")
            offset += len(batch)
            if offset >= page.get("count", offset + 1):
                break
        if expected_count is not None and len(points) != expected_count:
            raise ValueError("Des points Wemap manquent après déduplication des pages")
        return list(points.values())

    async def extract(self, map_id=DEFAULT_MAP_ID, zoom=None, log=print):
        livemap = await self.json(f"{API_URL}/livemaps/{map_id}", "raw/livemap.json")
        style_url = livemap["snippet"]["tilesstyle"]
        style = await self.json(style_url, "raw/style.json")
        sources = {
            name: source
            for name, source in style["sources"].items()
            if source.get("type") in ("vector", "geojson")
            and ("indoor" in name.lower() or source.get("bounds"))
        }
        if not sources:
            raise ValueError("Aucune source de plans intérieurs dans le style Wemap")
        features = []
        source_manifest = []
        for name, source in sources.items():
            cache_name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
            if source.get("url"):
                source = {
                    **source,
                    **await self.json(
                        urljoin(style_url, source["url"]),
                        f"raw/{cache_name}-tilejson.json",
                    ),
                }
            if source["type"] == "geojson":
                data = source["data"]
                data = (
                    await self.json(
                        urljoin(style_url, data), f"raw/{cache_name}.geojson"
                    )
                    if isinstance(data, str)
                    else data
                )
                for feature in data["features"]:
                    p = feature.setdefault("properties", {})
                    feature["id"] = f"{name}:{feature.get('id', len(features))}"
                    p.update(
                        {
                            "source": "wemap",
                            "source_feature_id": feature["id"],
                            "kind": feature_kind(p, feature["geometry"]["type"]),
                            "levels": feature_levels(p),
                        }
                    )
                    features.append(feature)
                continue
            native_zoom = int(source.get("maxzoom", 19))
            selected_zoom = native_zoom if zoom is None else min(int(zoom), native_zoom)
            if selected_zoom < int(source.get("minzoom", 0)):
                raise ValueError("Niveau de détail inférieur au minimum de la source")
            bounds = source["bounds"]
            coordinates = tile_coordinates(bounds, selected_zoom)
            if len(coordinates) > 2048:
                raise ValueError(
                    "Emprise trop grande : plus de 2 048 tuiles à extraire"
                )
            templates = source["tiles"]
            log(
                f"Wemap : {len(coordinates)} tuiles, niveau de détail {selected_zoom} ({name})"
            )

            async def tile(coordinate):
                z, x, y = coordinate
                tile_y = 2**z - 1 - y if source.get("scheme") == "tms" else y
                url = urljoin(style_url, templates[(x + y) % len(templates)]).format(
                    z=z, x=x, y=tile_y
                )
                return coordinate, await self.download(
                    url, f"raw/tiles/{cache_name}/{z}/{x}/{y}.pbf", allow_missing=True
                )

            tiles = await asyncio.gather(
                *(tile(coordinate) for coordinate in coordinates)
            )
            features.extend(decode_tiles(tiles, selected_zoom))
            source_manifest.append(
                {
                    "name": name,
                    "bounds": bounds,
                    "zoom": selected_zoom,
                    "tile_count": len(tiles),
                    "empty_tiles": sum(not payload for _, payload in tiles),
                    "attribution": source.get("attribution"),
                }
            )

        pinpoints = await self.paginated_pinpoints(map_id)
        write_json(self.directory / "raw/pinpoints.json", pinpoints)
        features = enrich_features(features, pinpoints, livemap)
        if not features or not any(
            f["properties"].get("kind") == "room" for f in features
        ):
            raise ValueError(
                "Aucun contour de salle extrait ; le dernier plan valide est conservé"
            )
        manifest = {
            "schema_version": 1,
            "source": "wemap",
            "style_url": style_url,
            "livemap_url": f"https://livemap.getwemap.com/dom?emmid={map_id}",
            "livemap_id": map_id,
            "name": livemap["name"],
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "source_updated_at": livemap.get("updated"),
            "sources": source_manifest,
            "pinpoint_count": len(pinpoints),
            "feature_count": len(features),
            "kinds": dict(Counter(f["properties"]["kind"] for f in features)),
            "geometry_types": dict(Counter(f["geometry"]["type"] for f in features)),
        }
        collection = {
            "type": "FeatureCollection",
            "name": livemap["name"],
            "metadata": manifest,
            "features": features,
        }
        write_json(self.directory / "campus.geojson", collection)
        write_json(self.directory / "manifest.json", manifest)
        write_json(GEO_PLANS_DIR / "campus.geojson", collection)
        log(
            f"Wemap : {manifest['feature_count']} géométries, dont {manifest['kinds']['room']} salles ; {len(pinpoints)} points d’intérêt"
        )
        return collection


async def scrape_wemap(progress=None, task_id=None, config=None, log=print):
    config = config or {}
    async with httpx.AsyncClient(
        timeout=45,
        follow_redirects=True,
        headers={
            "User-Agent": "PalantINT campus map importer",
            "Referer": f"https://livemap.getwemap.com/dom?emmid={config.get('wemap_id', DEFAULT_MAP_ID)}",
        },
    ) as client:
        extractor = WemapExtractor(
            client,
            concurrency=config.get("concurrency", 5),
            offline=config.get("wemap_offline", False),
        )
        return await extractor.extract(
            map_id=config.get("wemap_id", DEFAULT_MAP_ID),
            zoom=config.get("wemap_zoom"),
            log=log,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extraire les plans Wemap en GeoJSON (WGS84)"
    )
    parser.add_argument("--map-id", type=int, default=DEFAULT_MAP_ID)
    parser.add_argument("--zoom", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Reconstruire depuis les réponses et tuiles archivées",
    )
    args = parser.parse_args(argv)
    asyncio.run(
        scrape_wemap(
            config={
                "wemap_id": args.map_id,
                "wemap_zoom": args.zoom,
                "concurrency": args.concurrency,
                "wemap_offline": args.offline,
            }
        )
    )


if __name__ == "__main__":
    main()
