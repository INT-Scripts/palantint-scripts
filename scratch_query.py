import asyncio
from db.database import AsyncSessionLocal
from db.models import ApartmentDetail
from sqlalchemy.future import select

async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ApartmentDetail))
        details = result.scalars().all()
        print(f"ApartmentDetail records in DB: {len(details)}")
        if details:
            d = details[0]
            print(f"Sample: id={d.id}, building={d.building}, floor={d.floor}, type={d.type}, surface={d.surface}, price={d.price}")

if __name__ == "__main__":
    asyncio.run(main())
