import asyncio
import json
import os
import sys
import uuid
from datetime import datetime

from sqlalchemy.future import select
from unidecode import unidecode

import sys
# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from db.database import AsyncSessionLocal
from db.models import AgendaEvent, Student, StudentAgendaEvent

AGENDA_DIR = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../")), "data", "scraps", "agenda"
)


def normalize_name(name_str):
    """Normalize names for matching: uppercase, remove accents, remove extra spaces and hyphens."""
    if not name_str:
        return ""
    # "LE HEN - - GOLDMANN Tobias" -> "LE HENGOLDMANN TOBIAS"
    # Actually, people tend to have hyphens in db or spaces. Let's just remove all non-alphanumeric chars for robust matching?
    # Or just replace hyphens with spaces and remove multiple spaces.
    n = unidecode(name_str).upper()
    n = n.replace("-", " ").replace("'", " ")
    return " ".join(n.split())


async def get_all_students(session):
    stmt = select(Student)
    result = await session.execute(stmt)
    students = result.scalars().all()

    # Create a lookup mapping.
    # Mappings from normalized "LAST FIRST" and "FIRST LAST" to student.id
    lookup = {}
    for s in students:
        first = normalize_name(s.first_name)
        last = normalize_name(s.last_name)

        lookup[f"{last} {first}"] = s.id
        lookup[f"{first} {last}"] = s.id

        # sometimes names are glued or have extra parts
        lookup[f"{last}{first}"] = s.id
        lookup[f"{first}{last}"] = s.id

    return lookup


async def import_event(session, event_data, student_lookup):
    event_ref_id = event_data.get("id")
    if not event_ref_id:
        return

    # Check if event exists
    stmt = select(AgendaEvent).filter_by(event_ref_id=event_ref_id)
    result = await session.execute(stmt)
    event = result.scalars().first()

    date_str = event_data.get("date")
    start_time_str = event_data.get("start_time", "00h00").replace("h", ":")
    end_time_str = event_data.get("end_time", "00h00").replace("h", ":")

    try:
        start_time = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
        end_time = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        print(
            f"Skipping event {event_ref_id} due to invalid dates {date_str} {start_time_str}-{end_time_str}"
        )
        return

    professors_list = event_data.get("trainers") or []
    professors_str = (
        ", ".join(professors_list)
        if isinstance(professors_list, list)
        else str(professors_list)
    )

    is_new = False
    if not event:
        event = AgendaEvent(
            id=uuid.uuid4(),
            event_ref_id=event_ref_id,
            calendar_id=event_data.get("calendar_id", ""),
            name=event_data.get("name", "Unknown Event"),
            type=event_data.get("type", "Unknown Type"),
            start_time=start_time,
            end_time=end_time,
            room=event_data.get("room"),
            description=None,  # Not present in JSON usually
            professors=professors_str,
        )
        session.add(event)
        is_new = True
    else:
        # Update details
        event.name = event_data.get("name", "Unknown Event")
        event.type = event_data.get("type", "Unknown Type")
        event.start_time = start_time
        event.end_time = end_time
        event.room = event_data.get("room")
        event.professors = professors_str

    # Flush to get event.id
    if is_new:
        await session.flush()

    # Process students
    student_names = event_data.get("students")
    if student_names and isinstance(student_names, list):
        # Find matches
        matched_student_ids = set()
        for name_raw in student_names:
            norm_name = normalize_name(name_raw)
            # Find in lookup
            student_id = student_lookup.get(norm_name)

            # If not direct match, try replacing spaces since "LE HEN - - GOLDMANN" might compress
            if not student_id:
                compressed_name = norm_name.replace(" ", "")
                student_id = student_lookup.get(compressed_name)

            if student_id:
                matched_student_ids.add(student_id)
            else:
                pass

        # Handle mappings
        if not is_new:
            from sqlalchemy import delete

            await session.execute(
                delete(StudentAgendaEvent).where(
                    StudentAgendaEvent.event_id == event.id
                )
            )

        new_mappings = [
            StudentAgendaEvent(student_id=s_id, event_id=event.id)
            for s_id in matched_student_ids
        ]
        session.add_all(new_mappings)


async def process_file(session, file_path, student_lookup):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            events = json.load(f)

        if not isinstance(events, list):
            print(f"Skipping {file_path}, root is not a list")
            return 0

        print(f"Processing {len(events)} events from {os.path.basename(file_path)}...")
        count = 0
        for event_data in events:
            await import_event(session, event_data, student_lookup)
            count += 1
            if count % 200 == 0:
                await session.flush()

        await session.commit()
        print(f"Committed {count} events from {os.path.basename(file_path)}")
        return count
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        await session.rollback()
        return 0


async def main():
    async with AsyncSessionLocal() as session:
        print("Loading students into memory for matching...")
        student_lookup = await get_all_students(session)
        print(f"Loaded {len(student_lookup)} student variations.")

        index_path = os.path.join(AGENDA_DIR, "index.json")
        if not os.path.exists(index_path):
            print(f"Index file not found: {index_path}")
            return

        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)

        total_events = 0
        for cal_id, cal_info in index_data.items():
            file_name = cal_info.get("file")
            if not file_name:
                continue

            file_path = os.path.join(AGENDA_DIR, file_name)
            if not os.path.exists(file_path):
                print(f"File not found: {file_path}")
                continue

            total_events += await process_file(session, file_path, student_lookup)

        print(f"Import complete! Processed {total_events} events total.")


if __name__ == "__main__":
    asyncio.run(main())
