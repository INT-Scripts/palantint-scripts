import asyncio
import os
import sys

# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../backend/src")))

from sqlmodel import select
from core.auth import get_password_hash
from db.database import AsyncSessionLocal
from db.models import User

def main():
    if len(sys.argv) != 3:
        print("Usage: palantint-admin <username> <password>")
        sys.exit(1)
    asyncio.run(create_first_admin(sys.argv[1], sys.argv[2]))


async def create_first_admin(username, password):
    async with AsyncSessionLocal() as session:
        # Check if user already exists
        result = await session.execute(select(User).where(User.username == username))
        existing_user = result.scalars().first()

        if existing_user:
            print(f"User '{username}' already exists. Updating password to admin.")
            existing_user.hashed_password = get_password_hash(password)
            existing_user.is_admin = True
        else:
            print(f"Creating new admin user '{username}'...")
            admin_user = User(
                username=username,
                hashed_password=get_password_hash(password),
                is_admin=True,
            )
            session.add(admin_user)

        await session.commit()
        print("Success! You can now log in.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python create_admin.py <username> <password>")
        sys.exit(1)

    asyncio.run(create_first_admin(sys.argv[1], sys.argv[2]))
