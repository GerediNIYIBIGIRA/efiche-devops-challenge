import os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

_engine = None
_SessionFactory = None


def _get_session_factory():
    global _engine, _SessionFactory
    if _SessionFactory is None:
        DATABASE_URL = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5433/efiche_dev"
        )
        _engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        _SessionFactory = async_sessionmaker(_engine, expire_on_commit=False)
    return _SessionFactory


async def get_db():
    factory = _get_session_factory()
    async with factory() as session:
        yield session