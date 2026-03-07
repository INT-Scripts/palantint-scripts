import asyncio
import json
import os
from casint import CASClient
from trombint.client import TrombINT

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../data/scraps"))
ETUDIANTS_URL = "https://trombi.imtbs-tsp.eu/etudiants.php"

ECOLE_LABELS = {
    "IMT-BS": "Institut Mines-Télécom Business School",
    "TSP": "Télécom SudParis",
}
ANNEE_LABELS = {
    "bac_1": "Bachelor 1ère année",
    "bac_2": "Bachelor 2ème année",
    "bac_3": "Bachelor 3ème année",
    "fi_1": "Ingénieur 1ère année",
    "fi_2": "Ingénieur 2ème année",
    "fi_3": "Ingénieur 3ème année",
    "fi_ACI": "Année de Césure Internationale",
    "fm_MS": "Mastère Spécialisé",
    "fm_MSc": "Master of Science",
    "fm_MBA": "Executive MBA",
    "fm_DNM": "Diplôme National de Master",
    "doc": "Doctorant",
}

async def extract_backfill(cas_client: CASClient, progress=None, task_id=None, delay: float = 0.2):
    t_client = TrombINT(cookies=cas_client.cookies)
    student_data = {}

    total_searches = len(ECOLE_LABELS) + len(ANNEE_LABELS)
    if progress and task_id:
        progress.update(task_id, description="  [blue]Extract Backfill: Searching students...[/blue]", total=total_searches)
        progress.update(task_id, completed=0)

    # Search by école
    for code, label in ECOLE_LABELS.items():
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Extract Backfill: école={code}...[/blue]")
        data = {"etu[user]": "", "etu[ecole]": code, "etu[annee]": ""}
        async with t_client.get_client() as client:
            res = await client.post(ETUDIANTS_URL, data=data)
            students = t_client.parse_students(res.text)
        for s in students:
            uid = s.get("uid")
            if uid:
                if uid not in student_data: student_data[uid] = {}
                student_data[uid]["ecole"] = label
        if progress and task_id: progress.update(task_id, advance=1)
        if delay > 0: await asyncio.sleep(delay)

    # Search by année
    for code, label in ANNEE_LABELS.items():
        if progress and task_id:
            progress.update(task_id, description=f"  [blue]Extract Backfill: année={code}...[/blue]")
        data = {"etu[user]": "", "etu[ecole]": "", "etu[annee]": code}
        async with t_client.get_client() as client:
            res = await client.post(ETUDIANTS_URL, data=data)
            students = t_client.parse_students(res.text)
        for s in students:
            uid = s.get("uid")
            if uid:
                if uid not in student_data: student_data[uid] = {}
                student_data[uid]["promo"] = label
        if progress and task_id: progress.update(task_id, advance=1)
        if delay > 0: await asyncio.sleep(delay)

    output_path = os.path.join(DATA_DIR, "backfill.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(student_data, f, indent=4, ensure_ascii=False)

    if progress and task_id:
        progress.update(task_id, description=f"  [green]Extract Backfill: Saved {len(student_data)} tags to JSON.[/green]", completed=total_searches)

async def main():
    import getpass
    username = input("Enter CAS Username: ")
    password = getpass.getpass("Enter CAS Password: ")
    cas_client = CASClient(service_url=ETUDIANTS_URL)
    await cas_client.login(username=username, password=password)
    await extract_backfill(cas_client)

if __name__ == "__main__":
    asyncio.run(main())
