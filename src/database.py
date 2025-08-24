from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def init_db():
    # create tables via metadata.create_all
    from .models import User, Group, Whisper, Pending, Watch
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
