"""Background scheduler for periodic YouTube data refresh."""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Creator
from app.youtube import sync_creator_youtube

log = logging.getLogger("elusive.scheduler")

scheduler = AsyncIOScheduler()


async def refresh_all_creators():
    """Pull fresh YouTube stats for every active creator."""
    log.info("Starting scheduled refresh for all creators")
    async with async_session() as db:
        result = await db.execute(
            select(Creator).where(Creator.is_active == True)
        )
        creators = result.scalars().all()

        yt_ok = 0
        failed = 0
        for creator in creators:
            # Eagerly load the user relationship
            await db.refresh(creator, ["user"])

            # YouTube sync
            if await sync_creator_youtube(creator, db):
                yt_ok += 1
            else:
                failed += 1

        log.info(f"Refresh complete: YouTube {yt_ok} ok, {failed} failed")


def start_scheduler():
    """Start the background scheduler."""
    scheduler.add_job(
        refresh_all_creators,
        trigger=IntervalTrigger(hours=settings.YOUTUBE_REFRESH_HOURS),
        id="youtube_refresh",
        name="Refresh YouTube stats for all creators",
        replace_existing=True,
    )
    scheduler.start()
    log.info(f"Scheduler started: data refresh every {settings.YOUTUBE_REFRESH_HOURS}h")


def stop_scheduler():
    """Gracefully stop the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
