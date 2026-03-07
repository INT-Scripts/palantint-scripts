import asyncio
import json
import os
from datetime import datetime
from trombint.client import TrombINT
from casint import CASClient

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../data/scraps"))
os.makedirs(DATA_DIR, exist_ok=True)

async def extract_trombint(cas_client: CASClient, progress=None, task_id=None, delay: float = 0.1):
    if progress and task_id:
        progress.update(task_id, description="  [blue]Extract TrombINT: Initializing...[/blue]")

    t_client = TrombINT(cookies=cas_client.cookies)
    
    if progress and task_id:
        progress.update(task_id, description="  [blue]Extract TrombINT: Fetching raw student list...[/blue]")
    
    all_students_data = await t_client.get_all_students()
    
    # Save to JSON
    output_path = os.path.join(DATA_DIR, "trombint.json")
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_students_data, f, indent=4, ensure_ascii=False)
        
    if progress and task_id:
        progress.update(task_id, description=f"  [green]Extract TrombINT: Saved {len(all_students_data)} records to JSON.[/green]", completed=1, total=1)
        
    return output_path

async def main():
    import getpass
    username = input("Enter CAS Username: ")
    password = getpass.getpass("Enter CAS Password: ")
    cas_client = CASClient(service_url="https://cas6.imtbs-tsp.eu/cas/login")
    await cas_client.login(username=username, password=password)
    await extract_trombint(cas_client)

if __name__ == "__main__":
    asyncio.run(main())
