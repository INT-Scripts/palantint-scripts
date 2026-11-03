import json

import httpx
import mapbox_vector_tile
import pytest
from shapely.geometry import Polygon, box, shape

from palantint_scripts.scrapers.wemap import (
    WemapExtractor,
    decode_tiles,
    enrich_features,
    feature_levels,
)


def encoded(geometry, properties=None):
    return mapbox_vector_tile.encode(
        {
            "name": "features",
            "features": [
                {
                    "geometry": geometry,
                    "properties": properties
                    or {
                        "pk": 12,
                        "type": "indoorfeature",
                        "indoor": "room",
                        "level": "0",
                    },
                    "id": 12,
                }
            ],
        },
        default_options={"y_coord_down": True},
    )


def test_tile_seams_are_stitched_and_room_holes_are_preserved():
    tiles = [
        ((19, 265702, 180881), encoded(box(3500, 100, 4096, 1000))),
        ((19, 265703, 180881), encoded(box(0, 100, 500, 1000))),
    ]
    features = decode_tiles(tiles, 19)
    assert len(features) == 1
    assert features[0]["geometry"]["type"] == "Polygon"
    polygon = Polygon(
        [(100, 100), (1000, 100), (1000, 1000), (100, 1000)],
        holes=[[(300, 300), (300, 600), (600, 600), (600, 300)]],
    )
    room = decode_tiles([((19, 265702, 180881), encoded(polygon))], 19)[0]
    assert len(shape(room["geometry"]).interiors) == 1
    assert shape(room["geometry"]).is_valid


def test_names_join_only_by_source_id_and_unmatched_pois_survive():
    features = decode_tiles(
        [
            (
                (19, 265702, 180881),
                encoded(
                    box(100, 100, 1000, 1000),
                    {
                        "pk": 12,
                        "type": "indoorfeature",
                        "indoor": "room",
                        "pinpoint": 55,
                        "level": "0;1",
                    },
                ),
            )
        ],
        19,
    )
    pois = [
        {
            "id": 55,
            "name": "A101",
            "latitude": 48.625,
            "longitude": 2.443,
            "tags": ["class"],
            "opening_hours": "08:00-20:00",
        },
        {
            "id": 56,
            "name": "Entrée",
            "latitude": 48.624,
            "longitude": 2.442,
            "level": None,
        },
    ]
    result = enrich_features(
        features,
        pois,
        {
            "id": 29608,
            "snippet": {
                "usetags": {"tags": [{"slug": "class", "name": "Salle de classe"}]}
            },
        },
    )
    room = result[0]
    assert room["geometry"]["type"] == "Polygon"
    assert room["properties"]["name"] == "A101"
    assert room["properties"]["levels"] == [0, 1]
    assert room["properties"]["tag_names"] == ["Salle de classe"]
    assert room["properties"]["geometry_area_m2"] > 0
    assert result[1]["properties"]["name"] == "Entrée"


def test_fractional_and_multiple_levels_are_preserved():
    assert feature_levels({"level": "-0.5;0.5;1"}) == [-0.5, 0.5, 1]
    assert feature_levels({"min_level": -1, "max_level": 2}) == [-1, 0, 1, 2]
    assert feature_levels({}) == []


@pytest.mark.asyncio
async def test_pinpoint_pagination_uses_total_not_livemap_display_count(tmp_path):
    def response(request):
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200,
            json={
                "count": 3,
                "results": [{"id": n} for n in range(offset, min(offset + 2, 3))],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        points = await WemapExtractor(client, tmp_path).paginated_pinpoints(29608)
    assert [point["id"] for point in points] == [0, 1, 2]


@pytest.mark.asyncio
async def test_repeated_page_fails_instead_of_silently_truncating(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"count": 10, "results": [{"id": 1}]}
            )
        )
    ) as client:
        with pytest.raises(ValueError, match="répétée"):
            await WemapExtractor(client, tmp_path).paginated_pinpoints(29608)


@pytest.mark.asyncio
async def test_empty_tiles_cache_and_failed_extract_preserves_published_plan(tmp_path):
    published = tmp_path / "campus.geojson"
    published.write_text(json.dumps({"previous": True}))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as client:
        extractor = WemapExtractor(client, tmp_path)
        assert (
            await extractor.download(
                "https://example.test/empty.pbf", "raw/empty.pbf", allow_missing=True
            )
            == b""
        )
        assert (tmp_path / "raw/empty.pbf").exists()

    def response(request):
        if "/livemaps/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": 29608,
                    "snippet": {"tilesstyle": "https://example.test/style.json"},
                },
            )
        return httpx.Response(200, json={"sources": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ValueError, match="Aucune source"):
            await WemapExtractor(client, tmp_path).extract()
    assert json.loads(published.read_text()) == {"previous": True}
