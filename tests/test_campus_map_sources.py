import json
import math
import struct

import numpy as np

from palantint_scripts.plan_registration import Frame, apply, fit_control_points, fit_labels, fit_similarity, match_shape
from palantint_scripts.scrapers.basemap import to_features
from palantint_scripts.loaders.plan_georeference import placement
from palantint_scripts.scrapers.ign import relief_mesh, surface_grid, write_glb


def similarity(scale, degrees, tx, ty):
    angle = math.radians(degrees)
    return np.array([scale * math.cos(angle), scale * math.sin(angle), tx, ty])


def test_label_fit_recovers_transform_and_ignores_a_mismatched_room_number():
    truth = similarity(0.05, -63, 12.0, -40.0)
    plan_points = [(0, 0), (400, 0), (400, 300), (0, 300), (200, 150), (120, 260)]
    metric = apply(truth, plan_points)
    pairs = [(p, tuple(m), 2.0) for p, m in zip(plan_points, metric)]
    # A homonymous room elsewhere on the campus must not bend the fit.
    pairs.append(((300, 40), (500.0, 500.0), 2.0))
    fit = fit_labels(pairs)
    assert fit["method"] == "room_labels"
    assert fit["anchors"] == len(plan_points)
    assert fit["rms_m"] < 1e-6
    assert fit["level"] == 2.0
    assert np.allclose(fit["transform"], truth)


def test_label_fit_refuses_too_few_or_inconsistent_anchors():
    assert fit_labels([((0, 0), (0, 0), 0), ((1, 0), (1, 0), 0)]) is None
    scattered = [((x, y), (x * 7.0 % 13, y * 3.0 % 11), 0) for x, y in [(0, 0), (10, 0), (0, 10), (10, 10), (5, 3)]]
    assert fit_labels(scattered) is None


def test_shape_match_places_an_asymmetric_floor_and_flags_a_symmetric_one():
    # An L-shaped floor of rooms: a unique orientation.
    rooms = [[(x, y), (x + 40, y), (x + 40, y + 60), (x, y + 60)] for x, y in [(0, 0), (40, 0), (80, 0), (0, 60), (0, 120)]]
    truth = similarity(0.05, 30, 5.0, 8.0)
    target = [apply(truth, ring) for ring in rooms]
    scales = [0.045, 0.05, 0.055]
    match = match_shape(rooms, target, scales)
    assert match["mean_m"] < 0.2
    assert np.allclose(match["transform"], truth, atol=0.05)
    assert match["ambiguity"] < 0.8
    # A plain rectangle fits equally well upside down: it must be reported as ambiguous.
    square = [[(0, 0), (100, 0), (100, 50), (0, 50)]]
    assert match_shape(square, [apply(truth, square[0])], scales)["ambiguity"] > 0.8


def test_similarity_fit_is_exact_for_noise_free_points():
    truth = similarity(2.0, 10, 1.0, 2.0)
    points = [(0, 0), (1, 0), (0, 1)]
    assert np.allclose(fit_similarity(points, apply(truth, points)), truth)


def test_manual_control_points_are_fitted_privately_and_checked():
    frame = Frame([2.44, 48.62])
    truth = similarity(0.055, -128, 30.0, 40.0)
    plan_points = [(0, 0), (600, 0), (600, 700), (100, 650)]

    def geographic(xy):
        x, y = xy
        return [frame.origin[0] + x / (frame.k * math.cos(math.radians(frame.origin[1]))), frame.origin[1] - y / frame.k]

    points = [{"plan": list(p), "geographic": geographic(m)} for p, m in zip(plan_points, apply(truth, plan_points))]
    fit, reason = fit_control_points(points, frame)
    assert reason is None
    assert (fit["method"], fit["anchors"]) == ("control_points", 4)
    assert fit["rms_m"] < 1e-3
    assert np.allclose(fit["transform"], truth, atol=1e-6)
    assert fit_control_points(points[:2], frame) == (None, "Moins de trois points de contrôle manuels.")
    # A point 20 m off cannot be absorbed by a similarity: the plan stays unplaced.
    points[3]["geographic"] = geographic(apply(truth, [plan_points[3]])[0] + 20)
    fit, reason = fit_control_points(points, frame)
    assert fit is None and reason.startswith("Points de contrôle manuels incohérents")


def test_osm_elements_become_typed_features():
    elements = [
        {"type": "way", "id": 1, "tags": {"highway": "residential", "name": "Rue Charles Fourier"}, "geometry": [{"lon": 2.44, "lat": 48.62}, {"lon": 2.441, "lat": 48.621}]},
        {"type": "way", "id": 2, "tags": {"building": "apartments", "name": "U7", "building:levels": "6"}, "geometry": [{"lon": 2.44, "lat": 48.62}, {"lon": 2.441, "lat": 48.62}, {"lon": 2.441, "lat": 48.621}, {"lon": 2.44, "lat": 48.62}]},
        {"type": "way", "id": 3, "tags": {"highway": "footway"}, "geometry": [{"lon": 2.44, "lat": 48.62}, {"lon": 2.441, "lat": 48.62}, {"lon": 2.441, "lat": 48.621}, {"lon": 2.44, "lat": 48.62}]},
        {"type": "node", "id": 4, "tags": {"amenity": "bench"}, "lon": 2.44, "lat": 48.62},
        {"type": "way", "id": 5, "tags": {"barrier": "fence"}, "geometry": [{"lon": 2.44, "lat": 48.62}, {"lon": 2.441, "lat": 48.62}]},
    ]
    features = {feature["id"]: feature for feature in to_features(elements)}
    assert set(features) == {"way/1", "way/2", "way/3"}
    assert features["way/1"]["properties"]["class"] == "minor"
    assert features["way/2"]["geometry"]["type"] == "Polygon"
    assert features["way/2"]["properties"]["building_levels"] == "6"
    # A closed footway is still a path, not an area.
    assert features["way/3"]["geometry"]["type"] == "LineString"
    assert features["way/3"]["properties"]["class"] == "path"


def test_relief_tile_is_a_valid_unlit_glb_facing_up(tmp_path):
    grid = np.zeros((5, 5), np.float32)
    grid[2, 2] = 10
    positions, uvs, indices = relief_mesh(grid, 100.0)
    vertices = positions.reshape(-1, 3)
    assert vertices[12].tolist() == [50.0, 10.0, 50.0]
    assert uvs.reshape(-1, 2)[24].tolist() == [1.0, 1.0]
    a, b, c = (vertices[i] for i in indices[:3])
    assert np.cross(b - a, c - a)[1] > 0
    path = tmp_path / "tile.glb"
    write_glb(path, positions, uvs, indices, b"\xff\xd8jpeg")
    data = path.read_bytes()
    magic, version, length = struct.unpack("<III", data[:12])
    assert (magic, version, length) == (0x46546C67, 2, len(data))
    size = struct.unpack("<I", data[12:16])[0]
    document = json.loads(data[20:20 + size])
    assert document["extensionsUsed"] == ["KHR_materials_unlit"]
    assert document["accessors"][0]["max"] == [100.0, 10.0, 100.0]
    assert document["images"][0]["mimeType"] == "image/jpeg"


def test_placement_keeps_method_and_frame_or_records_why_a_plan_is_unplaced():
    registration = {
        "created_at": "2026-11-10T00:00:00+00:00",
        "frame": {"origin": [2.44, 48.62], "axes": "east_south_metres"},
        "plans": {"U1_1": {"building": "U1", "floor": "1", "method": "room_labels", "level": 1.0, "transform": {"a": 0.05, "b": 0, "tx": 0, "ty": 0}}},
        "unplaced": {"U6_1": "Aucun bâtiment géographique correspondant."},
    }
    placed = placement("U1_1", registration, "source")
    assert placed["status"] == "placed" and placed["frame"]["origin"] == [2.44, 48.62] and placed["source_id"] == "source"
    assert placement("U6_1", registration, "source") == {"status": "unplaced", "reason": "Aucun bâtiment géographique correspondant.", "source_id": "source", "computed_at": "2026-11-10T00:00:00+00:00"}
    assert placement("U9_1", registration, "source")["status"] == "unplaced"


def test_surface_combines_terrain_and_smoothed_height_above_ground():
    terrain = np.full((7, 7), 90.0, np.float32)
    terrain[:, 4:] = 70.0
    above = np.zeros((7, 7), np.float32)
    above[3, 2] = 30.0  # isolated spike: removed by the median
    above[1:6, 4:6] = 12.0  # a building block: kept
    above[0, 0] = -9999.0  # no data
    surface = surface_grid(terrain, above, reference=90.0)
    assert surface.shape == (5, 5)
    assert surface[2, 1] == 0.0
    assert surface[2, 3] == -20.0 + 12.0
    assert surface[0, 0] == 0.0
