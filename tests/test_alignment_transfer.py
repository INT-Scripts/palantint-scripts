import json

import pytest
from palantint_scripts.alignment_transfer import import_alignments
from sqlalchemy import select

from db.models import MapMetadata, ThreeDConfig


@pytest.mark.asyncio
async def test_alignment_import_backs_up_preserves_and_validates(db_session, tmp_path):
    previous = ThreeDConfig(
        key="default",
        tile_mappings={"kept": "a"},
        markers=[{"id": "local", "bldg_id": "U1"}],
    )
    db_session.add(previous)
    await db_session.flush()
    data = {
        "schema_version": 1,
        "source": "https://portal.bananint.fr",
        "config": {
            "tile_mappings": {},
            "markers": [
                {
                    "id": "remote",
                    "bldg_id": "U7",
                    "wireframe": {
                        "position": [3, 4, 5],
                        "rotation": [0, 0.5, 0],
                        "scale": 8,
                        "floor_height": 0.4,
                    },
                }
            ],
        },
        "maps": [
            {
                "building_id": "U7",
                "floor_id": "1",
                "pillars": [{"x": 10, "y": 20}, {"x": 80, "y": 60}],
            }
        ],
    }
    backup = await import_alignments(db_session, data, tmp_path)
    assert (
        json.loads((backup / "alignments.json").read_text())["config"]["markers"][0][
            "id"
        ]
        == "local"
    )
    assert {m["id"] for m in previous.markers} == {"local", "remote"}
    assert previous.tile_mappings == {"kept": "a"}
    assert previous.markers[1]["wireframe"]["rotation"] == [0, 0.5, 0]
    assert (
        await db_session.execute(select(MapMetadata))
    ).scalars().one().pillars == data["maps"][0]["pillars"]
    invalid = {
        **data,
        "maps": [{"building_id": "NO_SUCH_BUILDING", "floor_id": "1", "pillars": []}],
    }
    with pytest.raises(ValueError, match="inconnu"):
        await import_alignments(db_session, invalid, tmp_path)
    assert len(list(tmp_path.iterdir())) == 1
