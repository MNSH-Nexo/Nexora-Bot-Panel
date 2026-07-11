"""
database/engine.py — ایجاد engine و session factory برای SQLAlchemy Async
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from .models import Base

# ساخت engine غیر‌همزمان
engine = create_async_engine(
    settings.db_url,
    echo=False,          # برای debug: True
    pool_pre_ping=True,  # بررسی connection قبل از استفاده
)

# factory برای ساخت session
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def init_db() -> None:
    """ایجاد تمام جداول اگر وجود نداشته باشند (برای توسعه)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
