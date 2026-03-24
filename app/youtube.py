"""YouTube Data API v3 + Analytics API client."""
import datetime
import logging
from typing import Optional

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_token, encrypt_token
from app.config import settings
from app.models import Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic

log = logging.getLogger("elusive.youtube")

YT_DATA_BASE = "https://www.googleapis.com/youtube/v3"
YT_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"
TOKEN_URL = "https://oauth2.googleapis.com/token"


async def _refresh_access_token(user: User, db: AsyncSession) -> Optional[str]:
    """Use refresh token to get a fresh access token."""
    refresh_token = decrypt_token(user.google_refresh_token or "")
    if not refresh_token:
        log.warning(f"No refresh token for user {user.email}")
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.post(TOKEN_URL, data={
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })

    if resp.status_code != 200:
        log.error(f"Token refresh failed for {user.email}: {resp.text}")
        return None

    data = resp.json()
    access_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)

    user.google_access_token = encrypt_token(access_token)
    user.google_token_expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    await db.commit()

    return access_token


async def _get_valid_token(user: User, db: AsyncSession) -> Optional[str]:
    """Get a valid access token, refreshing if needed."""
    if user.google_token_expiry and user.google_token_expiry > datetime.datetime.utcnow():
        token = decrypt_token(user.google_access_token or "")
        if token:
            return token
    return await _refresh_access_token(user, db)


async def _yt_get(token: str, endpoint: str, params: dict) -> Optional[dict]:
    """Make an authenticated GET to YouTube Data API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{YT_DATA_BASE}/{endpoint}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    if resp.status_code != 200:
        log.error(f"YouTube API error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


async def _yt_analytics_get(token: str, params: dict) -> Optional[dict]:
    """Make an authenticated GET to YouTube Analytics API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{YT_ANALYTICS_BASE}/reports",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    if resp.status_code != 200:
        log.error(f"YouTube Analytics API error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


async def sync_creator_youtube(creator: Creator, db: AsyncSession) -> bool:
    """Full YouTube sync for a single creator. Returns True on success."""
    user = creator.user
    token = await _get_valid_token(user, db)
    if not token:
        log.warning(f"Cannot sync YouTube for {creator.display_name}: no valid token")
        return False

    try:
        # 1. Channel info
        await _sync_channel_info(creator, token, db)

        # 2. Recent videos
        await _sync_recent_videos(creator, token, db)

        # 3. Analytics (daily stats for last 30 days)
        await _sync_daily_stats(creator, token, db)

        # 4. Demographics
        await _sync_demographics(creator, token, db)

        # 5. Calculate engagement rate and trend
        await _calculate_metrics(creator, db)

        creator.last_yt_sync = datetime.datetime.utcnow()
        await db.commit()
        log.info(f"YouTube sync complete for {creator.display_name}")
        return True

    except Exception as e:
        log.error(f"YouTube sync failed for {creator.display_name}: {e}")
        await db.rollback()
        return False


async def _sync_channel_info(creator: Creator, token: str, db: AsyncSession):
    """Pull channel-level metadata."""
    data = await _yt_get(token, "channels", {
        "part": "snippet,statistics",
        "mine": "true",
    })
    if not data or not data.get("items"):
        return

    channel = data["items"][0]
    creator.youtube_channel_id = channel["id"]
    creator.youtube_channel_title = channel["snippet"]["title"]
    creator.youtube_channel_url = f"https://youtube.com/channel/{channel['id']}"
    creator.avatar_url = channel["snippet"]["thumbnails"].get("default", {}).get("url", creator.avatar_url)

    stats = channel.get("statistics", {})
    creator.yt_subscribers = int(stats.get("subscriberCount", 0))
    creator.yt_total_views = int(stats.get("viewCount", 0))
    creator.yt_video_count = int(stats.get("videoCount", 0))


async def _sync_recent_videos(creator: Creator, token: str, db: AsyncSession):
    """Pull the 10 most recent videos with stats."""
    if not creator.youtube_channel_id:
        return

    # Get recent video IDs
    search_data = await _yt_get(token, "search", {
        "part": "id",
        "channelId": creator.youtube_channel_id,
        "order": "date",
        "maxResults": 10,
        "type": "video",
    })
    if not search_data or not search_data.get("items"):
        return

    video_ids = [item["id"]["videoId"] for item in search_data["items"] if "videoId" in item.get("id", {})]
    if not video_ids:
        return

    # Get video details
    videos_data = await _yt_get(token, "videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
    })
    if not videos_data or not videos_data.get("items"):
        return

    for item in videos_data["items"]:
        vid_id = item["id"]
        snippet = item["snippet"]
        stats = item.get("statistics", {})

        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        engagement = ((likes + comments) / views * 100) if views > 0 else 0.0

        # Parse duration (ISO 8601 duration)
        duration = _parse_duration(item.get("contentDetails", {}).get("duration", "PT0S"))

        # Upsert
        result = await db.execute(
            select(YouTubeVideo).where(YouTubeVideo.video_id == vid_id)
        )
        video = result.scalar_one_or_none()

        if video is None:
            video = YouTubeVideo(
                creator_id=creator.id,
                video_id=vid_id,
                title=snippet.get("title", ""),
                thumbnail_url=snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                published_at=_parse_datetime(snippet.get("publishedAt")),
                duration_seconds=duration,
                views=views,
                likes=likes,
                comments=comments,
                engagement_rate=engagement,
            )
            db.add(video)
        else:
            video.views = views
            video.likes = likes
            video.comments = comments
            video.engagement_rate = engagement
            video.last_updated = datetime.datetime.utcnow()


async def _sync_daily_stats(creator: Creator, token: str, db: AsyncSession):
    """Pull daily analytics for the last 30 days."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=30)

    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,subscribersGained,subscribersLost,estimatedMinutesWatched,averageViewDuration,likes,comments,shares",
        "dimensions": "day",
        "sort": "day",
    })
    if not data or not data.get("rows"):
        return

    for row in data["rows"]:
        day_str = row[0]
        day_date = datetime.datetime.strptime(day_str, "%Y-%m-%d")

        # Upsert daily stat
        result = await db.execute(
            select(YouTubeStat).where(
                YouTubeStat.creator_id == creator.id,
                YouTubeStat.date == day_date,
            )
        )
        stat = result.scalar_one_or_none()

        if stat is None:
            stat = YouTubeStat(creator_id=creator.id, date=day_date)
            db.add(stat)

        stat.views = int(row[1])
        stat.subscribers_gained = int(row[2])
        stat.subscribers_lost = int(row[3])
        stat.watch_time_minutes = float(row[4])
        stat.avg_view_duration = float(row[5])
        stat.likes = int(row[6])
        stat.comments = int(row[7])
        stat.shares = int(row[8])


async def _sync_demographics(creator: Creator, token: str, db: AsyncSession):
    """Pull audience demographics."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=90)

    # Clear old demographics
    await db.execute(
        delete(YouTubeDemographic).where(YouTubeDemographic.creator_id == creator.id)
    )

    for dimension in ["ageGroup", "gender", "country"]:
        metric = "viewerPercentage"
        data = await _yt_analytics_get(token, {
            "ids": "channel==MINE",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "metrics": metric,
            "dimensions": dimension,
            "sort": f"-{metric}",
            "maxResults": 10,
        })
        if not data or not data.get("rows"):
            continue

        for row in data["rows"]:
            demo = YouTubeDemographic(
                creator_id=creator.id,
                dimension=dimension,
                value=row[0],
                percentage=float(row[1]),
            )
            db.add(demo)


async def _calculate_metrics(creator: Creator, db: AsyncSession):
    """Calculate 30-day views, engagement rate, and trend direction."""
    result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator.id)
        .order_by(YouTubeStat.date.desc())
        .limit(30)
    )
    stats = result.scalars().all()

    if not stats:
        return

    total_views = sum(s.views for s in stats)
    total_likes = sum(s.likes for s in stats)
    total_comments = sum(s.comments for s in stats)
    total_watch_time = sum(s.watch_time_minutes for s in stats)

    creator.yt_30d_views = total_views
    creator.yt_engagement_rate = (
        ((total_likes + total_comments) / total_views * 100) if total_views > 0 else 0.0
    )
    creator.yt_avg_view_duration = (
        (total_watch_time * 60 / total_views) if total_views > 0 else 0.0
    )

    # Trend: compare last 15 days vs prior 15 days
    if len(stats) >= 20:
        recent = sum(s.views for s in stats[:15])
        prior = sum(s.views for s in stats[15:30])
        if prior > 0:
            change = (recent - prior) / prior
            if change > 0.05:
                creator.trend_direction = "growing"
            elif change < -0.05:
                creator.trend_direction = "declining"
            else:
                creator.trend_direction = "stable"


def _parse_duration(iso_duration: str) -> int:
    """Parse ISO 8601 duration (PT1H2M3S) to seconds."""
    import re
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    """Parse ISO datetime string."""
    if not dt_str:
        return None
    try:
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None
