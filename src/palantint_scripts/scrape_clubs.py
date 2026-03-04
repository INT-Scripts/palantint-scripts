import asyncio
import os
import sys

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import Club

URL = "https://bde-imtbs-tsp.fr/fr/associative/"


async def fetch_html(url: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def scrape_clubs(session: AsyncSession = None):
    print("Fetching BDE website...")
    html = await fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")

    clubs_data = []
    found_origins = set()

    # BDE Clubs are linked in sections
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if "/fr/associative/" in href and href.count("/") > 3:
            # e.g., /fr/associative/bde/absinthe/
            parts = [p for p in href.split("/") if p]
            if len(parts) >= 3:
                # e.g. ['fr', 'associative', 'bde', 'absinthe']
                assoc_origin = parts[2].upper()  # 'BDE', 'ASINT', 'BDA'

                name = link.text.strip()
                if "-" in name:
                    name = name.split("-")[0].strip()

                if name and not any(c["name"] == name for c in clubs_data):
                    clubs_data.append(
                        {
                            "name": name,
                            "association_of_origin": assoc_origin,
                            "description": link.text.strip(),
                        }
                    )
                    found_origins.add(assoc_origin)

    # For every unique association origin, create a root club for the association itself
    for origin in found_origins:
        # Avoid creating duplicates if there's already a club with EXACTLY the origin name
        if not any(c["name"] == origin for c in clubs_data):
            clubs_data.append(
                {
                    "name": origin,
                    "association_of_origin": "Bureau / Asso Centrale",
                    "description": f"Bureau des Etudiants / Association: {origin}",
                }
            )

    print(f"Scraped {len(clubs_data)} clubs/associations. Updating Database...")

    async def process_clubs(db_session: AsyncSession):
        from sqlalchemy.dialects.postgresql import insert

        for club_data in clubs_data:
            stmt = insert(Club).values(
                name=club_data["name"],
                association_of_origin=club_data["association_of_origin"],
                description=club_data["description"]
            )

            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["name"],
                set_={
                    "association_of_origin": stmt.excluded.association_of_origin,
                    "description": stmt.excluded.description
                }
            )
            await db_session.execute(upsert_stmt)

        await db_session.commit()

    if session:
        await process_clubs(session)
    else:
        async with AsyncSessionLocal() as local_session:
            await process_clubs(local_session)

    print("Clubs database seeded.")


if __name__ == "__main__":
    asyncio.run(scrape_clubs())
