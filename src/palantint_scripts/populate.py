import asyncio
import os
import sys

from sqlalchemy import select, text
from db.database import AsyncSessionLocal
from db.models import RelationshipType

async def default_relationships(context=None):
    """
    Standardized entry point using PipelineContext.
    """
    if context:
        db_session = context.db_session
    else:
        # Fallback for standalone
        session_ctx = AsyncSessionLocal()
        db_session = await session_ctx.__aenter__()

    # Enable unaccent extension
    try:
        await db_session.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent;"))
    except Exception as e:
        print(f"Warning: Could not create unaccent extension: {e}")

    defaults = [
        {"name": "Amis", "color": "#3b82f6"},
        {"name": "En couple", "color": "#ec4899"},
        {"name": "Ex", "color": "#ef4444"},
    ]

    for rt_data in defaults:
        result = await db_session.execute(
            select(RelationshipType).where(RelationshipType.name == rt_data["name"])
        )
        existing = result.scalars().first()
        if not existing:
            db_session.add(RelationshipType(**rt_data))

    await db_session.flush()
    
    if not context:
        await db_session.commit()
        await session_ctx.__aexit__(None, None, None)

async def main():
    await default_relationships()

if __name__ == "__main__":
    asyncio.run(main())
