import asyncio
import os
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import sys
# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import Student

# ── Config ──────────────────────────────────────────────────────────────────
# Resolve to the root uploads folder (PalantINT/backend_data/uploads/profiles)
PROFILES_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")),
    "data",
    "assets",
    "profiles",
)
os.makedirs(PROFILES_DIR, exist_ok=True)

ETUDIANTS_URL = "https://trombi.imtbs-tsp.eu/etudiants.php"
CAS_LOGIN_URL = "https://cas6.imtbs-tsp.eu/cas/login"

ECOLES = ["IMT-BS", "TSP"]
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

IMAGE_DELAY = 0.3  # seconds between image downloads (rate limiting)


# ── CAS Auth ────────────────────────────────────────────────────────────────
def _create_cas_session(username: str, password: str) -> requests.Session:
    """Create and authenticate a requests session via CAS (inline, no external module)."""
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )

    # Step 1: GET the CAS login page
    r = session.get(CAS_LOGIN_URL, timeout=15)
    r.raise_for_status()

    # Step 2: Loop through CAS forms (login, SAML relay, attribute release, etc.)
    max_steps = 10
    for step in range(max_steps):
        soup = BeautifulSoup(r.text, "html.parser")

        # Check for password field → it's the login form
        if soup.find("input", type="password"):
            form = soup.find("form")
            if not form:
                raise Exception("CAS login form not found")
            action = form.get("action", "")
            inputs = {
                inp.get("name"): inp.get("value", "")
                for inp in form.find_all("input")
                if inp.get("name")
            }
            inputs["username"] = username
            inputs["password"] = password
            from urllib.parse import urljoin

            next_url = urljoin(r.url, action) if action else r.url
            r = session.post(next_url, data=inputs, allow_redirects=True)

            # Check for CAS error
            err_soup = BeautifulSoup(r.text, "html.parser")
            err = (
                err_soup.find(class_="errors")
                or err_soup.find(class_="error")
                or err_soup.find("div", {"class": "alert-danger"})
            )
            if err:
                raise Exception(f"CAS login failed: {err.get_text(strip=True)}")
            continue

        # Check for auto-submit form (SAML relay)
        if (
            "document.forms[0].submit()" in r.text
            or "document.formul.submit()" in r.text
        ):
            form = soup.find("form")
            if form:
                action = form.get("action", "")
                inputs = {
                    inp.get("name"): inp.get("value", "")
                    for inp in form.find_all("input")
                    if inp.get("name")
                }
                from urllib.parse import urljoin

                next_url = urljoin(r.url, action)
                r = session.post(next_url, data=inputs)
                r.raise_for_status()
                continue

        # No more forms — we're authenticated
        break

    # Warm up the session on the trombi
    session.get(ETUDIANTS_URL)
    print("[AUTH] ✅ CAS authentication successful.")
    return session


# ── HTML Parsing (same logic as TrombINT module) ───────────────────────────
def _parse_students(html: str) -> list[dict]:
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
                original_url = link["href"]
                parsed = urlparse(original_url)
                params = parse_qs(parsed.query)
                uid = params.get("uid", [None])[0]
                if uid:
                    etudiant["uid"] = uid
                params["h"] = ["320"]
                params["w"] = ["240"]
                new_query = urlencode(params, doseq=True)
                new_url = urlunparse(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        parsed.params,
                        new_query,
                        parsed.fragment,
                    )
                )
                if not new_url.startswith("http"):
                    new_url = f"https://trombi.imtbs-tsp.eu/{new_url.lstrip('/')}"
                etudiant["photo_url"] = new_url

        info_div = fiche.find("div", class_="ldapInfo")
        if info_div:
            import re

            email_link = info_div.find("a", href=re.compile(r"^mailto:"))
            if email_link:
                etudiant["email"] = email_link.get_text(strip=True)
            ul = info_div.find("ul")
            if ul:
                etudiant["details"] = [
                    li.get_text(strip=True) for li in ul.find_all("li")
                ]

        if "nom_complet" in etudiant:
            etudiants.append(etudiant)
    return etudiants


# ── Main Scrape Function ───────────────────────────────────────────────────
async def scrape_trombint(
    cas_username: str, cas_password: str, session: AsyncSession = None
):
    print("=" * 60)
    print("  🔍  PalantINT — STUDENT SYNC")
    print("=" * 60)

    # ── Phase 1: Authenticate ──────────────────────────────────
    print("\n[PHASE 1/3] Authenticating with CAS...")
    cas_session = await asyncio.to_thread(
        _create_cas_session, cas_username, cas_password
    )

    # ── Phase 2: Exhaustive Student Search ─────────────────────
    print("\n[PHASE 2/3] Searching for students (exhaustive)...")
    seen_uids: dict[str, dict] = {}  # uid -> student dict (enriched)

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

    # 2a. Search by école
    for ecole in ECOLES:
        print(f"  📚 Searching école={ecole}...", end=" ", flush=True)
        data = {"etu[user]": "", "etu[ecole]": ecole, "etu[annee]": ""}
        res = cas_session.post(ETUDIANTS_URL, data=data)
        res.raise_for_status()
        students = _parse_students(res.text)
        new = 0
        for s in students:
            uid = s.get("uid")
            if uid and uid not in seen_uids:
                s["_ecole"] = ECOLE_LABELS.get(ecole, ecole)
                seen_uids[uid] = s
                new += 1
        print(f"found {len(students)} ({new} new) — total: {len(seen_uids)}")

    # 2b. Search by année
    for annee in ANNEES:
        print(f"  📅 Searching année={annee}...", end=" ", flush=True)
        data = {"etu[user]": "", "etu[ecole]": "", "etu[annee]": annee}
        res = cas_session.post(ETUDIANTS_URL, data=data)
        res.raise_for_status()
        students = _parse_students(res.text)
        new = 0
        for s in students:
            uid = s.get("uid")
            if uid:
                if uid not in seen_uids:
                    seen_uids[uid] = s
                    new += 1
                # Always tag the année (more specific than école)
                seen_uids[uid]["_annee"] = ANNEE_LABELS.get(annee, annee)
                # Infer école from email domain if not already set
                if not seen_uids[uid].get("_ecole"):
                    email = seen_uids[uid].get("email", "")
                    if "imt-bs" in email:
                        seen_uids[uid]["_ecole"] = ECOLE_LABELS["IMT-BS"]
                    elif "telecom-sudparis" in email:
                        seen_uids[uid]["_ecole"] = ECOLE_LABELS["TSP"]
        print(f"found {len(students)} ({new} new) — total: {len(seen_uids)}")

    all_students = list(seen_uids.values())
    print(f"\n  ✅ Search complete: {len(all_students)} unique students found.\n")

    # ── Helper: format name ────────────────────────────────────
    def _format_name(nom_complet: str):
        parts = nom_complet.split()
        first_parts, last_parts = [], []
        for p in parts:
            if p == p.upper() and len(p) > 1:
                last_parts.append(p.capitalize())
            elif last_parts:
                last_parts.append(p.capitalize())
            else:
                first_parts.append(p)
        return (" ".join(first_parts) or (parts[0] if parts else "")), (
            " ".join(last_parts) or ""
        )

    # ── Insert into DB (Idempotent Upsert) ─────────────────────
    async def process_students(db_session: AsyncSession):
        from sqlalchemy.dialects.postgresql import insert

        inserted = 0
        updated = 0

        # We'll batch these for efficiency
        for s in all_students:
            uid = (
                s.get("uid")
                or s.get("nom_complet", "unknown").replace(" ", "_").lower()
            )
            first_name, last_name = _format_name(s.get("nom_complet", ""))

            stmt = insert(Student).values(
                trombint_id=uid,
                first_name=first_name,
                last_name=last_name,
                promo=s.get("_annee", ""),
                ecole=s.get("_ecole", ""),
                email=s.get("email", ""),
                profile_picture_path=s.get("photo_url", ""),
            )

            # Define the update mapping for conflict
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["trombint_id"],
                set_={
                    "first_name": stmt.excluded.first_name,
                    "last_name": stmt.excluded.last_name,
                    "promo": stmt.excluded.promo,
                    "ecole": stmt.excluded.ecole,
                    "email": stmt.excluded.email,
                    "profile_picture_path": stmt.excluded.profile_picture_path,
                }
            )

            await db_session.execute(upsert_stmt)
            inserted += 1 # We'll just report total processed for now in this simple loop

        await db_session.commit()
        print(
            f"  💾 Database synchronized: Processed {len(all_students)} student records (Upsert)."
        )

    if session:
        await process_students(session)
    else:
        async with AsyncSessionLocal() as local_session:
            await process_students(local_session)

    # ── Phase 3: Download Profile Images ───────────────────────
    print("\n[PHASE 3/3] Downloading profile images...")
    students_with_photos = [
        s for s in all_students if s.get("photo_url") and s.get("uid")
    ]
    total = len(students_with_photos)
    downloaded = 0
    skipped = 0
    failed = 0

    for i, student in enumerate(students_with_photos, 1):
        uid = student["uid"]
        url = student["photo_url"]
        dest = os.path.join(PROFILES_DIR, f"{uid}.jpg")

        if os.path.exists(dest):
            skipped += 1
            if i % 100 == 0 or i == total:
                print(
                    f"  📸 [{i}/{total}] {skipped} cached, {downloaded} downloaded, {failed} failed",
                    flush=True,
                )
            continue

        try:
            headers = {"Referer": "https://trombi.imtbs-tsp.eu/etudiants.php"}
            img_res = cas_session.get(url, headers=headers, timeout=10)
            img_res.raise_for_status()
            with open(dest, "wb") as f:
                f.write(img_res.content)
            downloaded += 1
        except Exception:
            failed += 1

        # Progress log every 50 images or at the end
        if i % 50 == 0 or i == total:
            print(
                f"  📸 [{i}/{total}] {skipped} cached, {downloaded} downloaded, {failed} failed",
                flush=True,
            )

        # Rate limit
        time.sleep(IMAGE_DELAY)

    print(
        f"\n  ✅ Images complete: {downloaded} downloaded, {skipped} already cached, {failed} failed."
    )
    print("=" * 60)
    print("  🎉  SYNC FINISHED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(scrape_trombint("", ""))
