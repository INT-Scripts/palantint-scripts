"""Backfill students with proper école, filière, email, and name formatting.
Runs a CAS-authenticated search to tag each student with their école/année from the form."""

import asyncio
import os
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select

import sys
# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import Student

ETUDIANTS_URL = "https://trombi.imtbs-tsp.eu/etudiants.php"
CAS_LOGIN_URL = "https://cas6.imtbs-tsp.eu/cas/login"

CAS_USERNAME = os.getenv("CAS_USERNAME", "")
CAS_PASSWORD = os.getenv("CAS_PASSWORD", "")

ECOLES = ["IMT-BS", "TSP"]
ECOLE_LABELS = {
    "IMT-BS": "Institut Mines-Télécom Business School",
    "TSP": "Télécom SudParis",
}
ANNEES = [
    "bac_1",
    "bac_2",
    "bac_3",
    "fi_1",
    "fi_2",
    "fi_3",
    "fi_ACI",
    "fm_MS",
    "fm_MSc",
    "fm_MBA",
    "fm_DNM",
    "doc",
]
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


def parse_students(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    fiches = soup.find_all("div", class_="ldapFiche")
    etudiants = []
    for fiche in fiches:
        etudiant = {}
        nom_div = fiche.find("div", class_="ldapNom")
        if nom_div:
            etudiant["nom_complet"] = nom_div.get_text(strip=True)

        photo_div = fiche.find("div", class_="ldapPhoto")
        if photo_div:
            link = photo_div.find("a")
            if link and link.get("href"):
                parsed = urlparse(link["href"])
                params = parse_qs(parsed.query)
                uid = params.get("uid", [None])[0]
                if uid:
                    etudiant["uid"] = uid

        info_div = fiche.find("div", class_="ldapInfo")
        if info_div:
            email_link = info_div.find("a", href=re.compile(r"^mailto:"))
            if email_link:
                etudiant["email"] = email_link.get_text(strip=True)

        if "nom_complet" in etudiant:
            etudiants.append(etudiant)
    return etudiants


def format_name(nom_complet: str):
    """Split 'Ada Olivia AARTELA' into proper first_name='Ada Olivia' last_name='Aartela'"""
    parts = nom_complet.split()
    first_parts = []
    last_parts = []
    for p in parts:
        if p == p.upper() and len(p) > 1:
            last_parts.append(p.capitalize())
        else:
            if last_parts:
                last_parts.append(p.capitalize())
            else:
                first_parts.append(p)

    first_name = " ".join(first_parts) if first_parts else parts[0] if parts else ""
    last_name = " ".join(last_parts) if last_parts else ""
    return first_name, last_name


async def main():
    print("=" * 60)
    print("  BACKFILL: Enriching student data")
    print("=" * 60)

    # Phase 1: CAS login and search to tag each student
    print("\n[1/3] Logging in and searching to tag students...")
    cas_session = cas_login()

    # uid -> {ecole_code, ecole_label, annee_code, annee_label, email, nom_complet}
    student_data: dict[str, dict] = {}

    # Search by école
    for ecole in ECOLES:
        print(f"  📚 école={ecole}...", end=" ", flush=True)
        data = {"etu[user]": "", "etu[ecole]": ecole, "etu[annee]": ""}
        res = cas_session.post(ETUDIANTS_URL, data=data)
        res.raise_for_status()
        students = parse_students(res.text)
        for s in students:
            uid = s.get("uid")
            if uid and uid not in student_data:
                student_data[uid] = {
                    "ecole_code": ecole,
                    "ecole_label": ECOLE_LABELS.get(ecole, ecole),
                    "email": s.get("email", ""),
                    "nom_complet": s.get("nom_complet", ""),
                }
        print(f"{len(students)} found, {len(student_data)} unique total")

    # Search by année
    for annee in ANNEES:
        print(f"  📅 année={annee}...", end=" ", flush=True)
        data = {"etu[user]": "", "etu[ecole]": "", "etu[annee]": annee}
        res = cas_session.post(ETUDIANTS_URL, data=data)
        res.raise_for_status()
        students = parse_students(res.text)
        new = 0
        for s in students:
            uid = s.get("uid")
            if uid:
                if uid not in student_data:
                    student_data[uid] = {
                        "ecole_code": "",
                        "ecole_label": "",
                        "email": s.get("email", ""),
                        "nom_complet": s.get("nom_complet", ""),
                    }
                    new += 1
                # Always set the année (more specific tag)
                student_data[uid]["annee_code"] = annee
                student_data[uid]["annee_label"] = ANNEE_LABELS.get(annee, annee)
        print(f"{len(students)} found ({new} new), total: {len(student_data)}")

    print(f"\n  ✅ Tagged {len(student_data)} students total.\n")

    # For those without école from search, infer from email
    for uid, info in student_data.items():
        if not info.get("ecole_label"):
            email = info.get("email", "")
            if "imt-bs" in email:
                info["ecole_code"] = "IMT-BS"
                info["ecole_label"] = "Institut Mines-Télécom Business School"
            elif "telecom-sudparis" in email:
                info["ecole_code"] = "TSP"
                info["ecole_label"] = "Télécom SudParis"

    # Phase 2: Update DB
    print("[2/3] Updating database...")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Student))
        all_students = result.scalars().all()
        updated = 0

        for student in all_students:
            info = student_data.get(student.trombint_id)
            if not info:
                continue

            # Format name nicely
            nom = info.get("nom_complet", "")
            if nom:
                fn, ln = format_name(nom)
                student.first_name = fn
                student.last_name = ln

            # Set promo (filière label like "Ingénieur 1ère année")
            annee_label = info.get("annee_label", "")
            if annee_label:
                student.promo = annee_label

            # Set department (école label)
            ecole_label = info.get("ecole_label", "")
            if ecole_label:
                student.ecole = ecole_label

            # Set email
            email = info.get("email", "")
            if email:
                student.email = email

            updated += 1

        await db.commit()
        print(f"  💾 Updated {updated} / {len(all_students)} students.\n")

    # Phase 3: Summary
    print("[3/3] Sample results:")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Student).limit(5))
        for s in result.scalars().all():
            print(
                f"  {s.first_name} {s.last_name} | {s.promo} | {s.department} | {s.email}"
            )

    print("\n" + "=" * 60)
    print("  ✅ BACKFILL COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
