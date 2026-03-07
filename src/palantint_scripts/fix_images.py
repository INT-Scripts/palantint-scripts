import asyncio
import os
import sys
import time
from casint import CASClient
from trombint.client import TrombINT
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncSessionLocal
from db.models import Student

PROFILES_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")), 
    "data", "assets", "profiles"
)
os.makedirs(PROFILES_DIR, exist_ok=True)

async def fix_missing_images(context):
    """
    Standardized entry point using PipelineContext.
    """
    progress = context.progress
    task_id = context.task_id
    cas_client = context.cas_client
    db_session = context.db_session

    if progress and task_id:
        progress.update(task_id, description="  [blue]Fix Images: Identifying missing photos...[/blue]")

    t_client = await TrombINT.create()

    # 1. Get students with photo URLs from DB
    result = await db_session.execute(
        select(Student).where(Student.profile_picture_path != "")
    )
    students = result.scalars().all()

    # 2. Identify missing ones
    missing = []
    for s in students:
        path = os.path.join(PROFILES_DIR, f"{s.trombint_id}.jpg")
        if not os.path.exists(path):
            missing.append(s)

    if not missing:
        if progress and task_id:
            progress.update(task_id, description="  [green]Fix Images: All photos present.[/green]", completed=1, total=1)
        return

    if progress and task_id:
        progress.update(task_id, description=f"  [blue]Fix Images: Downloading {len(missing)} missing photos...[/blue]", total=len(missing), completed=0)

    delay = getattr(context, 'delay', 0.1)

    # 3. Download
    for i, s in enumerate(missing, 1):
        dest = os.path.join(PROFILES_DIR, f"{s.trombint_id}.jpg")
        try:
            await t_client.download_image(s.profile_picture_path, dest)
        except Exception:
            pass
        
        if progress and task_id:
            progress.update(task_id, advance=1)
        
        if delay > 0:
            await asyncio.sleep(delay)

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Fix Images: Done.[/green]")

async def main():
    import getpass
    username = input("Enter CAS Username: ")
    password = getpass.getpass("Enter CAS Password: ")
    cas_client = CASClient(service_url="https://trombi.imtbs-tsp.eu/etudiants.php")
    await cas_client.login(username=username, password=password)
    async with AsyncSessionLocal() as session:
        class MockContext:
            def __init__(self, cli, sess):
                self.cas_client = cli
                self.db_session = sess
                self.progress = None
                self.task_id = None
        await fix_missing_images(MockContext(cas_client, session))
        await session.commit()

if __name__ == "__main__":
    asyncio.run(main())
