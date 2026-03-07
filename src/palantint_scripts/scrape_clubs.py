import asyncio
import os
import sys

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import Club

URL = "https://bde-imtbs-tsp.fr/fr/associative/"

async def fetch_html(url: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

async def scrape_clubs(context):
    """
    Standardized entry point using PipelineContext.
    """
    progress = context.progress
    task_id = context.task_id
    db_session = context.db_session

    if progress and task_id:
        progress.update(task_id, description="  [blue]Clubs: Fetching website...[/blue]")
        
    html = await fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")

    clubs_data = []
    found_origins = set()

    links = soup.find_all("a", href=True)
    if progress and task_id:
        progress.update(task_id, description="  [blue]Clubs: Parsing links...[/blue]", total=len(links), completed=0)

    for link in links:
        if progress and task_id: progress.update(task_id, advance=1)
        href = link["href"]
        if "/fr/associative/" in href and href.count("/") > 3:
            parts = [p for p in href.split("/") if p]
            if len(parts) >= 3:
                assoc_origin = parts[2].upper()
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

    for origin in found_origins:
        if not any(c["name"] == origin for c in clubs_data):
            clubs_data.append(
                {
                    "name": origin,
                    "association_of_origin": "Bureau / Asso Centrale",
                    "description": f"Bureau des Etudiants / Association: {origin}",
                }
            )

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Clubs: Syncing {len(clubs_data)} clubs...[/blue]")

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
    
    await db_session.flush()

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Clubs: Done ({len(clubs_data)} clubs).[/green]")

async def main():
    async with AsyncSessionLocal() as session:
        class MockContext:
            def __init__(self, sess):
                self.db_session = sess
                self.progress = None
                self.task_id = None
                self.cas_client = None
        await scrape_clubs(MockContext(session))
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
