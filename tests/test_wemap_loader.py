import json

import pytest
from sqlalchemy import select

from datetime import datetime

from db.models import Event, IngestionRun, Location
from palantint_scripts.loaders import wemap
from palantint_scripts.loaders import apartments as apartments_loader


def feature(identity, kind, name, **properties):
    return {
        "type": "Feature",
        "id": identity,
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [[2.44, 48.62], [2.45, 48.62], [2.45, 48.63], [2.44, 48.62]]
            ],
        },
        "properties": {
            "kind": kind,
            "name": name,
            "building_id": 1343,
            "levels": [0],
            "pk": identity,
            **properties,
        },
    }


@pytest.mark.asyncio
async def test_repeat_import_preserves_housing_and_manual_fields(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(wemap, "WEMAP_DIR", tmp_path)
    building = (
        (
            await db_session.execute(
                select(Location).where(
                    Location.kind == "BUILDING", Location.code == "U3"
                )
            )
        )
        .scalars()
        .one()
    )
    building.name = "Nom conservé"
    await db_session.flush()
    apartment = Location(
        kind="APARTMENT",
        code="3050",
        parent_id=building.id,
        attributes={"surface": "22 m²", "notes": "Recherche manuelle"},
    )
    db_session.add(apartment)
    await db_session.flush()
    (tmp_path / "campus.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    feature("building:1343", "building", "U3"),
                    feature("room:1", "room", "3050"),
                ],
            }
        )
    )
    await wemap.load_wemap(db_session, log=lambda message: None)
    await wemap.load_wemap(db_session, log=lambda message: None)
    apartments = list(
        (
            await db_session.execute(
                select(Location).where(Location.kind == "APARTMENT")
            )
        ).scalars()
    )
    assert len(apartments) == 1
    assert apartments[0].id == apartment.id
    assert apartments[0].parent_id == building.id
    assert apartments[0].attributes["surface"] == "22 m²"
    assert apartments[0].attributes["notes"] == "Recherche manuelle"
    assert apartments[0].attributes["wemap"]["geometry"]["type"] == "Polygon"
    assert building.name == "Nom conservé"
    assert len(list((await db_session.execute(select(IngestionRun))).scalars())) == 2
    # A subsequent Maisel refresh must retain the imported map namespace.
    monkeypatch.setattr(apartments_loader, "SCRAPS_AUTO_DIR", tmp_path)
    monkeypatch.setattr(apartments_loader, "EXPORT_DIR", str(tmp_path))
    (tmp_path / "logements.json").write_text(
        json.dumps({"3050": {"Bâtiment": "U3", "Etage": "0", "Superficie": "22 m²"}})
    )
    await apartments_loader.load_apartments(db_session, log=lambda message: None)
    assert apartment.attributes["wemap"]["geometry"]["type"] == "Polygon"
    assert apartment.attributes["notes"] == "Recherche manuelle"


@pytest.mark.asyncio
async def test_grouped_buildings_are_not_mistaken_for_single_residence(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(wemap, "WEMAP_DIR", tmp_path)
    (tmp_path / "campus.geojson").write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    feature("building:1365", "building", "U1 U2 U4", building_id=1365),
                    feature("room:7", "room", "1004", building_id=1365),
                ],
            }
        )
    )
    await wemap.load_wemap(db_session, log=lambda message: None)
    buildings = list(
        (
            await db_session.execute(
                select(Location).where(Location.kind == "BUILDING")
            )
        ).scalars()
    )
    assert [
        building.code for building in buildings if building.attributes.get("wemap")
    ] == ["WEMAP-1365"]


@pytest.mark.asyncio
async def test_same_named_pieces_are_distinct_and_referenced_rooms_survive_removal(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(wemap, "WEMAP_DIR", tmp_path)
    data = {
        "type": "FeatureCollection",
        "features": [
            feature("b:9", "building", "Bâtiment A", building_id=9),
            feature("r:1", "room", "Salle commune", building_id=9),
            feature("r:2", "room", "Salle commune", building_id=9),
        ],
    }
    path = tmp_path / "campus.geojson"
    path.write_text(json.dumps(data))
    await wemap.load_wemap(db_session, log=lambda message: None)
    rooms = list(
        (
            await db_session.execute(select(Location).where(Location.kind == "ROOM"))
        ).scalars()
    )
    assert len(rooms) == 2
    assert len({room.code for room in rooms}) == 2
    room = next(
        room for room in rooms if "r:1" in room.attributes["wemap"]["feature_ids"]
    )
    event = Event(
        name="Cours conservé",
        location_id=room.id,
        start_time=datetime(2026, 10, 27, 9),
        end_time=datetime(2026, 10, 27, 10),
    )
    db_session.add(event)
    await db_session.flush()
    data["features"] = [item for item in data["features"] if item["id"] != "r:1"]
    path.write_text(json.dumps(data))
    await wemap.load_wemap(db_session, log=lambda message: None)
    assert room.is_active
    assert not room.attributes["wemap"]["active"]
    assert event.location_id == room.id


@pytest.mark.asyncio
async def test_curated_etoile_flows_through_ingestion_and_api_without_changing_geometry(
    db_session, tmp_path, monkeypatch
):
    from api.private import indoor
    from core.map_reference import reference_data

    rule = reference_data()["curations"][0]
    room = Location(
        kind="ROOM",
        code="AMPHI Etoile 300pl",
        name="AMPHI Etoile 300pl",
        attributes={"notes": "Recherche conservée"},
    )
    db_session.add(room)
    await db_session.flush()
    event = Event(
        name="Cours",
        location_id=room.id,
        start_time=datetime(2026, 10, 27, 9),
        end_time=datetime(2026, 10, 27, 10),
    )
    db_session.add(event)
    # Simulate a previous, uncurated import, with its own persistent identity.
    previous = Location(
        kind="ROOM",
        code="Ancien contour",
        attributes={
            "wemap": {"active": True, "feature_ids": [rule["primary_feature_id"]]}
        },
    )
    db_session.add(previous)
    await db_session.flush()
    source = {
        "type": "FeatureCollection",
        "features": [
            feature("building:1346", "building", "H", building_id=1346),
            feature(rule["primary_feature_id"], "room", "", building_id=1346),
        ],
    }
    for identity in rule["feature_ids"][1:]:
        point = feature(identity, "poi", "Amphi Etoile", building_id=None)
        point["geometry"] = {"type": "Point", "coordinates": [2.44, 48.62]}
        source["features"].append(point)
    monkeypatch.setattr(wemap, "WEMAP_DIR", tmp_path)
    monkeypatch.setattr(wemap, "SCRAPS_MANUAL_DIR", tmp_path / "manual")
    (tmp_path / "campus.geojson").write_text(json.dumps(source))
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "campus.geojson").write_text(json.dumps(source))
    monkeypatch.setattr(indoor.settings, "PRIVATE_ASSETS_DIR", tmp_path)
    for _ in range(2):
        await wemap.load_wemap(db_session, log=lambda _: None)
        await db_session.flush()
    assert room.attributes["notes"] == "Recherche conservée"
    assert event.location_id == room.id
    assert (await db_session.get(Location, previous.id)) is not None
    assert not previous.attributes["wemap"]["feature_ids"]
    assert set(room.attributes["wemap"]["feature_ids"]) == set(rule["feature_ids"])
    assert room.attributes["wemap"]["curation"]["source_code"] == "vault_manual"
    assert room.attributes["wemap"]["curation"]["source_id"]
    assert room.attributes["wemap"]["primary_feature_id"] == rule["primary_feature_id"]
    served = await indoor.geojson(db=db_session)
    polygon = next(
        f for f in served["features"] if f["id"] == rule["primary_feature_id"]
    )
    assert polygon["properties"]["location_id"] == str(room.id)
    assert polygon["properties"]["name"] == rule["properties"]["name"]
    assert polygon["geometry"] == source["features"][1]["geometry"]
    assert json.loads((tmp_path / "campus.geojson").read_text()) == source
    matches = await indoor.lookup(q="Amphi Étoile", db=db_session)
    assert [match["id"] for match in matches] == [str(room.id)]
