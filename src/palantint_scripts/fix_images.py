"""Find students with missing profile images and download them."""

import asyncio
import os
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select

import sys
# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import Student

PROFILES_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")), 
    "data", "assets", "profiles"
)
os.makedirs(PROFILES_DIR, exist_ok=True)

CAS_LOGIN_URL = "https://cas6.imtbs-tsp.eu/cas/login"
ETUDIANTS_URL = "https://trombi.imtbs-tsp.eu/etudiants.php"

CAS_USERNAME = os.getenv("CAS_USERNAME", "")
CAS_PASSWORD = os.getenv("CAS_PASSWORD", "")


def cas_login():
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    r = session.get(CAS_LOGIN_URL, timeout=15)
    r.raise_for_status()
    for _ in range(10):
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.find("input", type="password"):
            form = soup.find("form")
            action = form.get("action", "")
            inputs = {
                inp.get("name"): inp.get("value", "")
                for inp in form.find_all("input")
                if inp.get("name")
            }
            inputs["username"] = CAS_USERNAME
            inputs["password"] = CAS_PASSWORD
            next_url = urljoin(r.url, action) if action else r.url
            r = session.post(next_url, data=inputs, allow_redirects=True)
            continue
        if "document.forms[0].submit()" in r.text:
            form = soup.find("form")
            if form:
                action = form.get("action", "")
                inputs = {
                    inp.get("name"): inp.get("value", "")
                    for inp in form.find_all("input")
                    if inp.get("name")
                }
                r = session.post(urljoin(r.url, action), data=inputs)
                r.raise_for_status()
                continue
        break
    session.get(ETUDIANTS_URL)
    print("✅ CAS login OK")
    return session


async def main():
    # 1. Get all students with a photo URL from DB
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Student).where(Student.profile_picture_path != "")
        )
        students = result.scalars().all()

    print(f"Total students with photo URL in DB: {len(students)}")

    # 2. Find which ones are missing on disk
    missing = []
    for s in students:
        path = os.path.join(PROFILES_DIR, f"{s.trombint_id}.jpg")
        if not os.path.exists(path):
            missing.append(s)

    print(f"Missing images on disk: {len(missing)}")
    if not missing:
        print("Nothing to do!")
        return

    for s in missing:
        print(
            f"  - {s.first_name} {s.last_name} ({s.trombint_id}) -> {s.profile_picture_path}"
        )

    # 3. Download them
    session = cas_login()
    for i, s in enumerate(missing, 1):
        dest = os.path.join(PROFILES_DIR, f"{s.trombint_id}.jpg")
        try:
            headers = {"Referer": ETUDIANTS_URL}
            res = session.get(s.profile_picture_path, headers=headers, timeout=10)
            res.raise_for_status()
            with open(dest, "wb") as f:
                f.write(res.content)
            print(f"  ✅ [{i}/{len(missing)}] Downloaded {s.trombint_id}.jpg")
        except Exception as e:
            print(f"  ❌ [{i}/{len(missing)}] Failed {s.trombint_id}: {e}")
        time.sleep(0.3)

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
