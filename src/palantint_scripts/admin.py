import asyncio
import sys

from sqlmodel import select
from core.auth import get_password_hash
from db.database import AsyncSessionLocal, init_db
from db.models import User

async def create_first_admin(username, password):
    # Ensure database is initialized before starting
    print("Initializing database...")
    await init_db()
    
    async with AsyncSessionLocal() as session:
        try:
            # Check if user already exists
            result = await session.execute(select(User).where(User.username == username))
            existing_user = result.scalars().first()

            if existing_user:
                print(f"User '{username}' already exists. Updating password and ensuring admin role.")
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
        except Exception as e:
            await session.rollback()
            print(f"Error creating admin: {e}")
            # Don't exit if called from interactive menu
            if __name__ == "__main__":
                sys.exit(1)


def main():
    if len(sys.argv) != 3:
        print("Usage: palantint-admin <username> <password>")
        sys.exit(1)
    
    username = sys.argv[1]
    password = sys.argv[2]
    
    asyncio.run(create_first_admin(username, password))


if __name__ == "__main__":
    main()
