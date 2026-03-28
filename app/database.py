from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Handle both postgres URL formats
db_url = settings.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add new columns to existing tables if they don't exist yet
        # (create_all only creates new tables, not new columns)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn):
    """Add columns from YouTube API expansion that create_all won't add."""
    from sqlalchemy import text, inspect
    inspector = inspect(conn)

    # Map of table -> list of (column_name, column_sql_type, default)
    new_columns = {
        "youtube_stats": [
            ("impressions", "INTEGER", None),
            ("impressions_ctr", "FLOAT", None),
            ("unique_viewers", "INTEGER", None),
        ],
        "youtube_video_analytics": [
            ("impressions", "INTEGER", None),
            ("impressions_ctr", "FLOAT", None),
            ("shares", "INTEGER", None),
        ],
        "youtube_demographics": [
            ("avg_view_duration", "FLOAT", None),
        ],
        "youtube_videos": [
            ("shares", "INTEGER", None),
            ("tags", "TEXT", None),  # JSON stored as TEXT in Postgres
        ],
        "creators": [
            ("yt_impressions_30d", "INTEGER", "0"),
            ("yt_impressions_ctr", "FLOAT", "0.0"),
            ("yt_unique_viewers_30d", "INTEGER", "0"),
        ],
    }

    for table_name, columns in new_columns.items():
        try:
            existing = {c["name"] for c in inspector.get_columns(table_name)}
        except Exception:
            continue  # table doesn't exist yet — create_all will handle it

        for col_name, col_type, default in columns:
            if col_name not in existing:
                default_clause = f" DEFAULT {default}" if default is not None else ""
                conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}{default_clause}"
                ))
