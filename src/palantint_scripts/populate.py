import asyncio
import os
import sys

from sqlalchemy import select, text

import sys
# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import RelationshipType


async def default_relationships():
    async with AsyncSessionLocal() as session:
        # PostgreSQL: Enable unaccent extension globally for the database before doing anything
        try:
            await session.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent;"))
            await session.commit()
        except Exception as e:
            # Fails silently if user lacks permissions, but ideally works as a superuser
            print(
                "Could not create unaccent extension automatically (might require superuser permissions):",
                e,
            )

        # Default types to add if not present
        defaults = [
            {"name": "Amis", "color": "#3b82f6"},  # Blue
            {"name": "En couple", "color": "#ec4899"},  # Pink
            {"name": "Ex", "color": "#ef4444"},  # Red
        ]

        for rt_data in defaults:
            result = await session.execute(
                select(RelationshipType).where(RelationshipType.name == rt_data["name"])
            )
            existing = result.scalars().first()
            if not existing:
                print(f"Adding default relationship type: {rt_data['name']}")
                session.add(RelationshipType(**rt_data))

        await session.commit()
        print("Done")


if __name__ == "__main__":
    asyncio.run(default_relationships())
