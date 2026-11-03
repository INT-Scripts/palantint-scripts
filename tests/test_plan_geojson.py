import pytest
from palantint_scripts.map_gen import convert_svg


def source(tmp_path, body, name="U1_0.svg", view="0 0 100 100"):
    path = tmp_path / name
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}">{body}</svg>'
    )
    return path


def test_real_boundaries_and_holes_are_preserved_and_local_coordinates_are_not_fake_wgs84(
    tmp_path,
):
    path = source(
        tmp_path,
        '<path d="M 2 2 H 98 V 98 H 2 Z M 40 40 H 60 V 60 H 40 Z"/><text id="1001"><tspan x="20" y="20">1001</tspan></text>',
    )
    before = path.read_bytes()
    data = convert_svg(path)
    room = next(f for f in data["features"] if f["properties"]["kind"] == "room")
    assert room["properties"]["room_id"] == "1001"
    assert room["properties"]["extraction"] == "wall_enclosure"
    assert len(room["properties"]["plan_geometry"]["coordinates"]) == 2
    assert all(f["geometry"] is None for f in data["features"])
    assert data["metadata"]["plan_coordinate_space"] == "local_plan"
    assert not data["metadata"]["georeferenced"]
    assert path.read_bytes() == before
    assert convert_svg(path) == data


def test_open_or_ambiguous_rooms_keep_labels_and_never_invent_rectangles(tmp_path):
    path = source(
        tmp_path,
        '<path d="M 2 2 H 98 V 98 H 2 Z"/><text id="1001" x="20" y="20">1001</text><text id="1002" x="60" y="20">1002</text>',
    )
    data = convert_svg(path)
    rooms = [f for f in data["features"] if f["properties"]["kind"] == "room"]
    assert len(rooms) == 2
    assert all(f["properties"]["plan_geometry"]["type"] == "Point" for f in rooms)
    assert set(data["metadata"]["unresolved_rooms"]) == {"1001", "1002"}


def test_georeferencing_exports_true_geographic_geometry_when_control_points_exist(
    tmp_path,
):
    path = source(
        tmp_path,
        '<path d="M 2 2 H 98 V 98 H 2 Z"/><text id="1001" x="20" y="20">1001</text>',
    )
    points = [
        {"plan": [0, 0], "geographic": [2.44, 48.62]},
        {"plan": [100, 0], "geographic": [2.441, 48.62]},
        {"plan": [0, 100], "geographic": [2.44, 48.619]},
    ]
    data = convert_svg(path, control_points=points)
    assert data["metadata"]["georeferenced"]
    assert data["metadata"]["georeference"]["rms_error_m"] < 0.001
    assert all(f["geometry"] is not None for f in data["features"])
    point = data["features"][0]["geometry"]["coordinates"][0]
    assert 2.44 < point[0] < 2.441 and 48.619 < point[1] < 48.62
    with pytest.raises(ValueError):
        convert_svg(path, control_points=points[:2])
    bad = [{"plan": [i, i], "geographic": [2.44 + i * 0.001, 48.62]} for i in range(3)]
    with pytest.raises(ValueError):
        convert_svg(path, control_points=bad)
    collapsed = [{**point, "geographic": [2.44, 48.62]} for point in points]
    with pytest.raises(ValueError, match="géographiques sont alignés"):
        convert_svg(path, control_points=collapsed)


def test_nested_svg_transforms_and_nonzero_viewbox_are_retained(tmp_path):
    path = source(
        tmp_path,
        '<g transform="translate(10 20)"><path d="M 0 0 L 10 0"/><text id="1001" x="2" y="3">1001</text></g>',
        view="10 20 100 100",
    )
    data = convert_svg(path)
    assert data["features"][0]["properties"]["plan_geometry"]["coordinates"] == [
        [10, 20],
        [20, 20],
    ]
    assert data["features"][1]["properties"]["label_coordinates"] == [12, 23]
    assert data["metadata"]["view_box"] == [10, 20, 100, 100]
