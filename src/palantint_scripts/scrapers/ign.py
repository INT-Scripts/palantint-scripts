"""Harvest IGN imagery and LiDAR HD around the campus, then build the map products.

Harvest (``IGN_ARCHIVE_DIR``, rebuildable offline):

* ``aerial/{z}/{x}/{y}.jpg``: Web Mercator tiles of the 20 cm orthophoto
  (``HR.ORTHOIMAGERY.ORTHOPHOTOS``);
* ``heights/{z}/{x}/{y}.npz``: per z18 cell, the LiDAR HD terrain (``MNT``) and
  surface (``MNS``, roofs and canopy) elevation grids, 50 cm sources sampled on
  the mesh nodes. Both models are needed: the terrain gives the ground and the
  campus reference level, the surface the volumes the orthophoto is draped on.

Published (``GEO_PLANS_DIR``, served by the private map routes):

* ``aerial/``: the archived tiles, hard-linked (no second copy) with an index;
* ``relief/{z}/{x}/{y}.glb``: one textured mesh per cell. The surface is the
  terrain plus the smoothed height of the surface model above it, relative to the campus ground
  level, so the campus rests on the flat map and the surroundings keep their
  real relief.

Mesh coordinates are local to the cell's north-west corner: x east and z south
in EPSG:3857 metres, y up in real metres. The client converts them to MapLibre
mercator units. Data © IGN, Licence Ouverte 2.0 (Etalab).
"""

import argparse
import asyncio
import io
import json
import math
import struct
from datetime import datetime, timezone

import httpx
import numpy as np

from palantint_scripts.config import AERIAL_DIR, GEO_PLANS_DIR, IGN_ARCHIVE_DIR, RELIEF_DIR
from palantint_scripts.scrapers.basemap import bounds_around, campus_center
from palantint_scripts.scrapers.wemap import tile_coordinates, write_json

WMTS_URL = "https://data.geopf.fr/wmts"
WMS_URL = "https://data.geopf.fr/wms-r/wms"
AERIAL_LAYER = "HR.ORTHOIMAGERY.ORTHOPHOTOS"
SURFACE_LAYER = "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
TERRAIN_LAYER = "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93"
ARCHIVED_AERIAL = IGN_ARCHIVE_DIR / "aerial"
ARCHIVED_HEIGHTS = IGN_ARCHIVE_DIR / "heights"
USER_AGENT = "PalantINT campus map"
ATTRIBUTION = "© IGN"
EARTH = 2 * math.pi * 6378137
RELIEF_ZOOM = 18
# Half-side of the aerial coverage per zoom level, in metres.
AERIAL_RADIUS = {13: 6000, 14: 5000, 15: 4000, 16: 3000, 17: 2000, 18: 1500, 19: 1000}


def mercator_bounds(z, x, y):
    """EPSG:3857 (minx, miny, maxx, maxy) of an XYZ tile."""
    size = EARTH / 2**z
    minx = -EARTH / 2 + x * size
    maxy = EARTH / 2 - y * size
    return minx, maxy - size, minx + size, maxy


def lonlat_bounds(z, x, y):
    def lon(value):
        return value / 2**z * 360 - 180

    def lat(value):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * value / 2**z))))

    return [lon(x), lat(y + 1), lon(x + 1), lat(y)]


async def fetch(client, url, params, attempts=4):
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith(("image/", "application/octet")):
                raise httpx.HTTPError(f"Réponse inattendue : {response.text[:160]}")
            return response.content
        except httpx.HTTPError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(1.5 * (attempt + 1))


async def aerial_tile(client, z, x, y):
    path = ARCHIVED_AERIAL / str(z) / str(x) / f"{y}.jpg"
    if path.exists():
        return path
    content = await fetch(client, WMTS_URL, {
        "SERVICE": "WMTS", "REQUEST": "GetTile", "VERSION": "1.0.0", "LAYER": AERIAL_LAYER,
        "STYLE": "normal", "TILEMATRIXSET": "PM", "TILEMATRIX": z, "TILEROW": y, "TILECOL": x,
        "FORMAT": "image/jpeg",
    })
    if content is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def aerial_tiles(center):
    return [tile for z, radius in AERIAL_RADIUS.items() for tile in tile_coordinates(bounds_around(center, radius), z)]


async def download_aerial(client, tiles, log, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def one(tile):
        nonlocal done
        async with semaphore:
            await aerial_tile(client, *tile)
        done += 1
        if done % 250 == 0 or done == len(tiles):
            log(f"  Photographies aériennes : {done}/{len(tiles)} tuiles")

    await asyncio.gather(*(one(tile) for tile in tiles))


def publish_aerial(center, log):
    """Expose the archived tiles to the map routes without duplicating them on disk."""
    import os
    import shutil

    published = 0
    for source in ARCHIVED_AERIAL.rglob("*.jpg"):
        target = AERIAL_DIR / source.relative_to(ARCHIVED_AERIAL)
        if target.exists() and target.stat().st_ino == source.stat().st_ino:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        published += 1
    write_json(AERIAL_DIR / "index.json", {
        "source": "ign", "layer": AERIAL_LAYER, "attribution": ATTRIBUTION, "license": "Licence Ouverte 2.0",
        "tile_size": 256, "minzoom": min(AERIAL_RADIUS), "maxzoom": max(AERIAL_RADIUS),
        "bounds": [round(value, 7) for value in bounds_around(center, AERIAL_RADIUS[max(AERIAL_RADIUS)])],
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    })
    log(f"  Photographies aériennes publiées ({published} nouvelles tuiles)")


async def wms_grid(client, layer, z, x, y, samples):
    """A layer sampled at the n×n cell nodes plus one node of margin on each side.

    WMS pixel centres sit exactly on the nodes, so adjacent cells share their
    edge samples, and the margin lets filters see the neighbouring cell.
    """
    minx, miny, maxx, maxy = mercator_bounds(z, x, y)
    step = (maxx - minx) / (samples - 1)
    half = step / 2 + step
    size = samples + 2
    content = await fetch(client, WMS_URL, {
        "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": layer, "STYLES": "",
        "CRS": "EPSG:3857", "BBOX": f"{minx - half},{miny - half},{maxx + half},{maxy + half}",
        "WIDTH": size, "HEIGHT": size, "FORMAT": "image/x-bil;bits=32",
    })
    return np.frombuffer(content, dtype="<f4").reshape(size, size).astype(np.float32)


async def harvest_heights(client, z, x, y, samples):
    path = ARCHIVED_HEIGHTS / str(z) / str(x) / f"{y}.npz"
    if path.exists():
        with np.load(path) as archived:
            if "mns" in archived and archived["mns"].shape[0] == samples + 2:
                return path
    terrain = await wms_grid(client, TERRAIN_LAYER, z, x, y, samples)
    surface = await wms_grid(client, SURFACE_LAYER, z, x, y, samples)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, mnt=terrain, mns=surface)
    return path


def surface_grid(terrain, surface, reference):
    """Terrain plus the smoothed height of the surface model above it, relative to the campus ground level.

    The 3×3 median removes isolated canopy and noise spikes from the height
    above ground; it uses the archived margin, so shared edges stay identical.
    """
    size = surface.shape[0]
    known = np.isfinite(terrain) & (terrain > -100) & (terrain < 5000) & np.isfinite(surface) & (surface > -100) & (surface < 5000)
    above = np.where(known, surface - terrain, 0)
    above = np.where((above > -5) & (above < 250), above, 0)
    above = np.clip(above, 0, None)
    neighbours = [above[1 + dy:size - 1 + dy, 1 + dx:size - 1 + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    smoothed = np.median(np.stack(neighbours), axis=0)
    ground = terrain[1:-1, 1:-1]
    valid = np.isfinite(ground) & (ground > -100) & (ground < 5000)
    ground = np.where(valid, ground, np.median(ground[valid]) if valid.any() else reference)
    return (ground + smoothed - reference).astype(np.float32)


def campus_ground(center):
    """Terrain elevation at the campus centre, from the archived cell that contains it."""
    (z, x, y), = tile_coordinates((center[0], center[1], center[0] + 1e-9, center[1] + 1e-9), RELIEF_ZOOM)
    with np.load(ARCHIVED_HEIGHTS / str(z) / str(x) / f"{y}.npz") as archived:
        terrain = archived["mnt"]
    valid = terrain[np.isfinite(terrain) & (terrain > -100) & (terrain < 5000)]
    return float(np.median(valid))


def cell_texture(z, x, y, size):
    """Stitch the four z+1 orthophoto tiles of a cell into one JPEG texture."""
    from PIL import Image

    image = Image.new("RGB", (512, 512), (24, 24, 27))
    for dx in (0, 1):
        for dy in (0, 1):
            path = ARCHIVED_AERIAL / str(z + 1) / str(2 * x + dx) / f"{2 * y + dy}.jpg"
            if path.exists():
                image.paste(Image.open(path).convert("RGB"), (dx * 256, dy * 256))
    if size != 512:
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=86, optimize=True)
    return buffer.getvalue()


def write_glb(path, positions, uvs, indices, texture):
    """Minimal binary glTF: one unlit textured mesh, quantized (KHR_mesh_quantization).

    Positions are 16-bit steps between the tile's bounds, restored by the node's
    scale and translation (about 2 mm horizontally on a detail cell); texture
    coordinates are normalized 16-bit. This more than halves the tiles.
    """
    vertices = positions.reshape(-1, 3).astype(np.float64)
    low, high = vertices.min(0), vertices.max(0)
    step = np.where(high > low, (high - low) / 65535, 1.0)
    quantized = np.zeros((len(vertices), 4), np.uint16)  # padded to 8 bytes per vertex
    quantized[:, :3] = np.round((vertices - low) / step)
    coordinates = np.round(uvs.reshape(-1, 2).astype(np.float64) * 65535).astype(np.uint16)
    chunks, views, offset = [], [], 0
    for data, target, stride in ((quantized, 34962, 8), (coordinates, 34962, None), (indices, 34963, None), (texture, None, None)):
        raw = data if isinstance(data, bytes) else data.tobytes()
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(raw)}
        if target:
            view["target"] = target
        if stride:
            view["byteStride"] = stride
        views.append(view)
        padding = (4 - len(raw) % 4) % 4
        chunks.append(raw + b"\0" * padding)
        offset += len(raw) + padding
    binary = b"".join(chunks)
    document = {
        "asset": {"version": "2.0", "generator": "PalantINT IGN relief"},
        "extensionsUsed": ["KHR_materials_unlit", "KHR_mesh_quantization"],
        "extensionsRequired": ["KHR_mesh_quantization"],
        "scene": 0, "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "translation": [float(v) for v in low], "scale": [float(v) for v in step]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0, "roughnessFactor": 1}, "extensions": {"KHR_materials_unlit": {}}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "images": [{"bufferView": 3, "mimeType": "image/jpeg"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5123, "count": len(vertices), "type": "VEC3",
             "min": [int(v) for v in quantized[:, :3].min(0)], "max": [int(v) for v in quantized[:, :3].max(0)]},
            {"bufferView": 1, "componentType": 5123, "normalized": True, "count": len(coordinates), "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123 if indices.dtype == np.uint16 else 5125, "count": len(indices), "type": "SCALAR"},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    text = json.dumps(document, separators=(",", ":")).encode()
    text += b" " * ((4 - len(text) % 4) % 4)
    total = 12 + 8 + len(text) + 8 + len(binary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<III", 0x46546C67, 2, total)
        + struct.pack("<II", len(text), 0x4E4F534A) + text
        + struct.pack("<II", len(binary), 0x004E4942) + binary
    )


def relief_mesh(grid, cell_size):
    samples = grid.shape[0]
    step = cell_size / (samples - 1)
    j, i = np.mgrid[0:samples, 0:samples]
    positions = np.stack([i * step, grid, j * step], axis=-1).astype(np.float32).reshape(-1)
    uvs = np.stack([i / (samples - 1), j / (samples - 1)], axis=-1).astype(np.float32).reshape(-1)
    node = (j * samples + i)[:-1, :-1]
    # Counter-clockwise when seen from above (+y), with z pointing south.
    quads = np.stack([node, node + samples, node + 1, node + 1, node + samples, node + samples + 1], axis=-1)
    indices = quads.reshape(-1).astype(np.uint16 if samples * samples <= 65536 else np.uint32)
    return positions, uvs, indices


def relief_cells(center, radius):
    detail = set(tile_coordinates(detail_extent(center), RELIEF_ZOOM))
    cells = sorted(set(tile_coordinates(bounds_around(center, radius), RELIEF_ZOOM)) | detail)
    return [(cell, "detail" if cell in detail else "context") for cell in cells]


# Detail cells (~100 m) get a node every ~0.4 m, close to the 50 cm LiDAR, so walls
# stand where the orthophoto shows them; context cells stay lighter. 256 nodes keep
# the vertex count at 65 536, so triangle indices stay 16-bit.
SAMPLES = {"detail": (256, 512), "context": (65, 256)}


async def harvest_relief(client, cells, log, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def one(cell, lod):
        nonlocal done
        async with semaphore:
            await harvest_heights(client, *cell, SAMPLES[lod][0])
        done += 1
        if done % 20 == 0 or done == len(cells):
            log(f"  Relief LiDAR : {done}/{len(cells)} cellules")

    await asyncio.gather(*(one(cell, lod) for cell, lod in cells))


def publish_relief(center, cells, log):
    """Rebuild every mesh from the archive; no network access."""
    reference = campus_ground(center)
    entries = []
    for (z, x, y), lod in cells:
        archive = ARCHIVED_HEIGHTS / str(z) / str(x) / f"{y}.npz"
        if not archive.exists():
            continue
        with np.load(archive) as archived:
            grid = surface_grid(archived["mnt"], archived["mns"], reference)
        minx, _, maxx, _ = mercator_bounds(z, x, y)
        positions, uvs, indices = relief_mesh(grid, maxx - minx)
        write_glb(RELIEF_DIR / str(z) / str(x) / f"{y}.glb", positions, uvs, indices, cell_texture(z, x, y, SAMPLES[lod][1]))
        entries.append({"z": z, "x": x, "y": y, "lod": lod, "path": f"{z}/{x}/{y}.glb", "bounds": [round(v, 8) for v in lonlat_bounds(z, x, y)]})
    write_json(RELIEF_DIR / "index.json", {
        "source": "ign", "terrain_layer": TERRAIN_LAYER, "surface_layer": SURFACE_LAYER, "texture_layer": AERIAL_LAYER,
        "attribution": ATTRIBUTION, "license": "Licence Ouverte 2.0",
        "coordinate_space": "cell_local_epsg3857_metres_y_up",
        "ground_reference_m": round(reference, 2),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "tiles": sorted(entries, key=lambda entry: (entry["x"], entry["y"])),
    })
    log(f"  Relief 3D publié : {len(entries)} tuiles, sol du campus à {reference:.1f} m NGF")


def detail_extent(center):
    """Wemap extraction bounds plus a margin; falls back to a square around the centre."""
    try:
        bounds = json.loads((GEO_PLANS_DIR / "campus.geojson").read_text(encoding="utf-8"))["metadata"]["sources"][0]["bounds"]
        margin_lon, margin_lat = 0.002, 0.0013
        return (bounds[0] - margin_lon, bounds[1] - margin_lat, bounds[2] + margin_lon, bounds[3] + margin_lat)
    except (OSError, KeyError, IndexError, ValueError):
        return bounds_around(center, 400)


async def scrape_ign(progress=None, task_id=None, config=None, log=print):
    """Harvest missing IGN tiles and grids into the archive, then publish the map products.

    ``config`` keys: ``ign_offline`` (rebuild from the archive only),
    ``ign_aerial`` / ``ign_relief`` (default True), ``ign_relief_radius`` (metres),
    ``concurrency``.
    """
    config = config or {}
    center = campus_center()
    cells = relief_cells(center, config.get("ign_relief_radius", 700))
    concurrency = min(config.get("concurrency", 6), 8)
    if not config.get("ign_offline"):
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60) as client:
            if config.get("ign_aerial", True):
                log("Téléchargement des photographies aériennes IGN (20 cm)…")
                await download_aerial(client, aerial_tiles(center), log, concurrency)
            if config.get("ign_relief", True):
                log("Téléchargement du LiDAR HD IGN (terrain et surface)…")
                await harvest_relief(client, cells, log, concurrency)
    if config.get("ign_aerial", True):
        publish_aerial(center, log)
    if config.get("ign_relief", True):
        publish_relief(center, cells, log)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Télécharger l’imagerie et le LiDAR IGN autour du campus, puis construire le relief 3D")
    parser.add_argument("--no-aerial", action="store_true", help="Ignorer les photographies aériennes")
    parser.add_argument("--no-relief", action="store_true", help="Ignorer le relief 3D")
    parser.add_argument("--relief-radius", type=int, default=700, help="Demi-côté du relief, en mètres")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--offline", action="store_true", help="Reconstruire depuis l’archive sans téléchargement")
    args = parser.parse_args(argv)
    asyncio.run(scrape_ign(config={"ign_offline": args.offline, "ign_aerial": not args.no_aerial, "ign_relief": not args.no_relief, "ign_relief_radius": args.relief_radius, "concurrency": args.concurrency}))


if __name__ == "__main__":
    main()
