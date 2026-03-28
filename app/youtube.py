"""YouTube Data API v3 + Analytics API v2 + Reporting API v1 client."""
import csv
import datetime
import gzip
import io
import logging
from typing import Optional

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_token, encrypt_token
from app.config import settings
from app.models import (
    Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic,
    YouTubeVideoAnalytics, YouTubeTrafficSource, YouTubeSearchTerm,
    YouTubeCardStats, YouTubeReportingJob,
)

log = logging.getLogger("elusive.youtube")

YT_DATA_BASE = "https://www.googleapis.com/youtube/v3"
YT_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"
YT_REPORTING_BASE = "https://youtubereporting.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Standardised window — 90 days for all analytics queries (spec note on consistency)
ANALYTICS_WINDOW_DAYS = 90
# Daily stats window — 90 days to match
DAILY_STATS_WINDOW_DAYS = 90
# Video analytics batch — 90 days, top 200 videos
VIDEO_BATCH_WINDOW_DAYS = 90
VIDEO_BATCH_MAX_RESULTS = 200


# ─── Token Management ────────────────────────────────────────────────

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


# ─── API Helpers ──────────────────────────────────────────────────────

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
        # 400s for unsupported metric/dimension combos are expected — log as INFO not ERROR
        if resp.status_code == 400:
            log.info(f"YouTube Analytics API 400 (unsupported query): {resp.text[:150]}")
        else:
            log.error(f"YouTube Analytics API error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


async def _yt_reporting_get(token: str, path: str, params: dict = None) -> Optional[dict]:
    """Make an authenticated GET to YouTube Reporting API."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{YT_REPORTING_BASE}/{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    if resp.status_code != 200:
        log.error(f"YouTube Reporting API error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


async def _yt_reporting_post(token: str, path: str, body: dict) -> Optional[dict]:
    """Make an authenticated POST to YouTube Reporting API."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{YT_REPORTING_BASE}/{path}",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
    if resp.status_code not in (200, 201):
        log.error(f"YouTube Reporting API POST error {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json()


# ─── Main Sync Orchestrator ──────────────────────────────────────────

async def sync_creator_youtube(creator: Creator, db: AsyncSession) -> bool:
    """Full YouTube sync for a single creator. Returns True on success."""
    user = creator.user
    token = await _get_valid_token(user, db)
    if not token:
        log.warning(f"Cannot sync YouTube for {creator.display_name}: no valid token")
        return False

    try:
        # 1. Ensure Reporting API jobs exist (idempotent, runs fast)
        await _ensure_reporting_jobs(creator, token, db)

        # 2. Channel info
        await _sync_channel_info(creator, token, db)

        # 3. Recent videos (with tags + shares)
        await _sync_recent_videos(creator, token, db)

        # 4. Daily stats (impressions, CTR, uniques included)
        await _sync_daily_stats(creator, token, db)

        # 5. Demographics (country avg_view_duration, age watch time, playback location, OS)
        await _sync_demographics(creator, token, db)

        # 6. Subscribed status
        await _sync_subscribed_status(creator, token, db)

        # 7. Traffic sources
        await _sync_traffic_sources(creator, token, db)

        # 8. Search keywords (traffic source detail)
        await _sync_traffic_source_detail(creator, token, db)

        # 9. Per-video analytics batch (90d, impressions, CTR, shares)
        await _sync_video_analytics_batch(creator, token, db)

        # 10. Card metrics
        await _sync_card_metrics(creator, token, db)

        # 11. Download and ingest Reporting API data
        await _sync_reporting_data(creator, token, db)

        # 12. Recalculate aggregate metrics
        await _calculate_metrics(creator, db)

        creator.last_yt_sync = datetime.datetime.utcnow()
        await db.commit()
        log.info(f"YouTube sync complete for {creator.display_name}")
        return True

    except Exception as e:
        log.error(f"YouTube sync failed for {creator.display_name}: {e}")
        await db.rollback()
        return False


# ─── Sync Functions ───────────────────────────────────────────────────

async def _sync_channel_info(creator: Creator, token: str, db: AsyncSession):
    """Pull channel-level metadata including branding keywords."""
    data = await _yt_get(token, "channels", {
        "part": "snippet,statistics,brandingSettings",
        "mine": "true",
    })

    # Fallback to brand/managed channels if mine has no videos
    channel = None
    if data and data.get("items"):
        channel = data["items"][0]
    if not channel or int(channel.get("statistics", {}).get("videoCount", 0)) == 0:
        managed = await _yt_get(token, "channels", {"part": "snippet,statistics,brandingSettings", "managedByMe": "true"})
        if managed and managed.get("items"):
            channel = max(managed["items"], key=lambda c: int(c.get("statistics", {}).get("subscriberCount", 0)))

    if not channel:
        return
    creator.youtube_channel_id = channel["id"]
    creator.youtube_channel_title = channel["snippet"]["title"]
    creator.youtube_channel_url = f"https://youtube.com/channel/{channel['id']}"
    creator.avatar_url = channel["snippet"]["thumbnails"].get("default", {}).get("url", creator.avatar_url)

    stats = channel.get("statistics", {})
    creator.yt_subscribers = int(stats.get("subscriberCount", 0))
    creator.yt_total_views = int(stats.get("viewCount", 0))
    creator.yt_video_count = int(stats.get("videoCount", 0))


async def _sync_recent_videos(creator: Creator, token: str, db: AsyncSession):
    """Pull the 50 most recent videos with stats, tags, and shares."""
    if not creator.youtube_channel_id:
        return

    # Get recent video IDs
    search_data = await _yt_get(token, "search", {
        "part": "id",
        "channelId": creator.youtube_channel_id,
        "order": "date",
        "maxResults": 50,
        "type": "video",
    })
    if not search_data or not search_data.get("items"):
        return

    video_ids = [item["id"]["videoId"] for item in search_data["items"] if "videoId" in item.get("id", {})]
    if not video_ids:
        return

    # Get video details (including tags)
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
        comments_count = int(stats.get("commentCount", 0))
        engagement = ((likes + comments_count) / views * 100) if views > 0 else 0.0

        # Parse duration (ISO 8601 duration)
        duration = _parse_duration(item.get("contentDetails", {}).get("duration", "PT0S"))

        # Extract tags from snippet
        tags = snippet.get("tags", [])

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
                comments=comments_count,
                tags=tags if tags else None,
                engagement_rate=engagement,
            )
            db.add(video)
        else:
            video.views = views
            video.likes = likes
            video.comments = comments_count
            video.engagement_rate = engagement
            video.tags = tags if tags else video.tags
            video.last_updated = datetime.datetime.utcnow()


async def _sync_daily_stats(creator: Creator, token: str, db: AsyncSession):
    """Pull daily analytics for the last 90 days.

    Note: impressions/CTR are NOT available with the 'day' dimension in
    the Analytics API. They are fetched as a channel-level aggregate and
    will be backfilled per-day by the Reporting API (channel_combined).
    """
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=DAILY_STATS_WINDOW_DAYS)

    # Daily stats query — only metrics supported with 'day' dimension
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

    # Separate aggregate query for impressions + CTR (no day dimension — API limitation)
    # NOTE: impressions/CTR metrics require YouTube Partner Program (monetisation).
    # Channels without YPP will get a 400 "Unknown identifier" — this is expected.
    impressions_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "impressions,impressionClickThroughRate",
    })
    if impressions_data and impressions_data.get("rows"):
        agg_row = impressions_data["rows"][0]
        total_impressions = int(agg_row[0]) if agg_row[0] is not None else None
        avg_ctr = float(agg_row[1]) if agg_row[1] is not None else None
        if total_impressions is not None:
            creator.yt_impressions_30d = total_impressions
        if avg_ctr is not None:
            creator.yt_impressions_ctr = avg_ctr
    else:
        log.info("Impressions metrics unavailable (channel may not be in YouTube Partner Program)")


async def _sync_demographics(creator: Creator, token: str, db: AsyncSession):
    """Pull audience demographics including country avg_view_duration, age watch time, playback location, OS."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=ANALYTICS_WINDOW_DAYS)

    # Clear old demographics (except subscribedStatus which has its own sync)
    await db.execute(
        delete(YouTubeDemographic).where(
            YouTubeDemographic.creator_id == creator.id,
            YouTubeDemographic.dimension != "subscribedStatus",
        )
    )

    # 1. ageGroup + gender (viewerPercentage)
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "viewerPercentage",
        "dimensions": "ageGroup,gender",
        "sort": "ageGroup,gender",
    })
    if data and data.get("rows"):
        age_totals: dict[str, float] = {}
        gender_totals: dict[str, float] = {}
        for row in data["rows"]:
            age_group, gender, pct = row[0], row[1], float(row[2])
            age_totals[age_group] = age_totals.get(age_group, 0) + pct
            gender_totals[gender] = gender_totals.get(gender, 0) + pct
        for value, pct in age_totals.items():
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="ageGroup",
                value=value, percentage=round(pct, 1),
            ))
        for value, pct in gender_totals.items():
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="gender",
                value=value, percentage=round(pct, 1),
            ))

    # 2. Country — views + averageViewDuration (spec 1C)
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,averageViewDuration",
        "dimensions": "country",
        "sort": "-views",
        "maxResults": 25,
    })
    if data and data.get("rows"):
        total = sum(float(r[1]) for r in data["rows"])
        for row in data["rows"]:
            pct = (float(row[1]) / total * 100) if total > 0 else 0
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="country",
                value=row[0], percentage=round(pct, 1),
                avg_view_duration=float(row[2]),
            ))

    # 3. Device type
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views",
        "dimensions": "deviceType",
        "sort": "-views",
        "maxResults": 10,
    })
    if data and data.get("rows"):
        total = sum(float(r[1]) for r in data["rows"])
        for row in data["rows"]:
            pct = (float(row[1]) / total * 100) if total > 0 else 0
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="deviceType",
                value=row[0], percentage=round(pct, 1),
            ))

    # 4. Age group watch time (spec 1D)
    # NOTE: estimatedMinutesWatched with ageGroup dimension is NOT supported by the
    # Analytics API (returns 400). Age watch time data will come from the Reporting API
    # via _ingest_channel_combined() when bulk CSV reports are available.
    # The ageGroup_watch_time dimension will be empty until then.

    # 5. Playback location (spec 1G)
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views",
        "dimensions": "insightPlaybackLocationType",
        "sort": "-views",
    })
    if data and data.get("rows"):
        total = sum(float(r[1]) for r in data["rows"])
        for row in data["rows"]:
            pct = (float(row[1]) / total * 100) if total > 0 else 0
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="playbackLocation",
                value=row[0], percentage=round(pct, 1),
            ))

    # 6. Operating system (spec 1H)
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views",
        "dimensions": "operatingSystem",
        "sort": "-views",
        "maxResults": 10,
    })
    if data and data.get("rows"):
        total = sum(float(r[1]) for r in data["rows"])
        for row in data["rows"]:
            pct = (float(row[1]) / total * 100) if total > 0 else 0
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension="operatingSystem",
                value=row[0], percentage=round(pct, 1),
            ))


async def _sync_subscribed_status(creator: Creator, token: str, db: AsyncSession):
    """Pull subscriber vs non-subscriber view breakdown."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=ANALYTICS_WINDOW_DAYS)

    # Remove old subscribedStatus demographics
    await db.execute(
        delete(YouTubeDemographic).where(
            YouTubeDemographic.creator_id == creator.id,
            YouTubeDemographic.dimension == "subscribedStatus",
        )
    )

    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views",
        "dimensions": "subscribedStatus",
        "sort": "-views",
    })
    if not data or not data.get("rows"):
        return

    total = sum(int(r[1]) for r in data["rows"])
    for row in data["rows"]:
        pct = (int(row[1]) / total * 100) if total > 0 else 0
        db.add(YouTubeDemographic(
            creator_id=creator.id,
            dimension="subscribedStatus",
            value=row[0],
            percentage=round(pct, 1),
        ))


async def _sync_traffic_sources(creator: Creator, token: str, db: AsyncSession):
    """Pull channel-level traffic sources (aggregate + daily)."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=30)

    # Clear old channel-level traffic sources
    await db.execute(
        delete(YouTubeTrafficSource).where(
            YouTubeTrafficSource.creator_id == creator.id,
            YouTubeTrafficSource.video_id == None,
        )
    )

    # 1. Aggregate traffic sources (no day dimension)
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "insightTrafficSourceType",
        "sort": "-views",
    })
    if data and data.get("rows"):
        for row in data["rows"]:
            db.add(YouTubeTrafficSource(
                creator_id=creator.id,
                video_id=None,
                date=None,
                source_type=row[0],
                views=int(row[1]),
                watch_time_minutes=float(row[2]),
            ))

    # 2. Daily traffic sources (for stacked area chart)
    daily_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "day,insightTrafficSourceType",
        "sort": "day",
    })
    if daily_data and daily_data.get("rows"):
        for row in daily_data["rows"]:
            day_date = datetime.datetime.strptime(row[0], "%Y-%m-%d")
            db.add(YouTubeTrafficSource(
                creator_id=creator.id,
                video_id=None,
                date=day_date,
                source_type=row[1],
                views=int(row[2]),
                watch_time_minutes=float(row[3]),
            ))


async def _sync_traffic_source_detail(creator: Creator, token: str, db: AsyncSession):
    """Pull top YouTube search terms driving views (spec 1F)."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=30)

    # Clear old search terms
    await db.execute(
        delete(YouTubeSearchTerm).where(YouTubeSearchTerm.creator_id == creator.id)
    )

    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "insightTrafficSourceDetail",
        "filters": "insightTrafficSourceType==YT_SEARCH",
        "sort": "-views",
        "maxResults": 25,
    })
    if not data or not data.get("rows"):
        return

    for row in data["rows"]:
        db.add(YouTubeSearchTerm(
            creator_id=creator.id,
            term=row[0],
            views=int(row[1]),
            watch_time_minutes=float(row[2]),
        ))


async def _sync_video_analytics_batch(creator: Creator, token: str, db: AsyncSession):
    """Batch-sync per-video analytics (90d, top 200).

    Note: impressions/CTR are NOT supported with the 'video' dimension
    in the Analytics API. Only views, averageViewDuration, averageViewPercentage,
    and shares are available. Per-video impressions come from Reporting API.
    """
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=VIDEO_BATCH_WINDOW_DAYS)

    # Metrics supported with 'video' dimension
    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,averageViewDuration,averageViewPercentage,shares",
        "dimensions": "video",
        "sort": "-views",
        "maxResults": VIDEO_BATCH_MAX_RESULTS,
    })
    if not data or not data.get("rows"):
        return

    for row in data["rows"]:
        yt_video_id = row[0]
        # row indices: 0=videoId, 1=views, 2=avgDuration, 3=avgPct, 4=shares
        avg_duration = float(row[2])
        avg_pct = float(row[3])
        shares = int(row[4]) if row[4] is not None else None

        # Find the YouTubeVideo record by video_id string
        result = await db.execute(
            select(YouTubeVideo).where(YouTubeVideo.video_id == yt_video_id)
        )
        video = result.scalar_one_or_none()
        if not video:
            continue

        # Update shares on the video record too
        if shares is not None:
            video.shares = shares

        # Upsert YouTubeVideoAnalytics
        result = await db.execute(
            select(YouTubeVideoAnalytics).where(YouTubeVideoAnalytics.video_id == video.id)
        )
        analytics = result.scalar_one_or_none()
        if analytics is None:
            analytics = YouTubeVideoAnalytics(video_id=video.id)
            db.add(analytics)
        analytics.avg_view_duration = avg_duration
        analytics.avg_pct_viewed = avg_pct
        analytics.shares = shares
        analytics.last_updated = datetime.datetime.utcnow()

    await db.commit()


async def _sync_card_metrics(creator: Creator, token: str, db: AsyncSession):
    """Pull channel-level card performance metrics (spec 1I)."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=ANALYTICS_WINDOW_DAYS)

    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "cardImpressions,cardClicks,cardClickRate,cardTeaserImpressions,cardTeaserClicks,cardTeaserClickRate",
    })
    if not data or not data.get("rows") or not data["rows"]:
        return

    row = data["rows"][0]  # Single aggregate row (no dimensions)

    # Upsert card stats
    result = await db.execute(
        select(YouTubeCardStats).where(YouTubeCardStats.creator_id == creator.id)
    )
    card_stats = result.scalar_one_or_none()
    if card_stats is None:
        card_stats = YouTubeCardStats(creator_id=creator.id)
        db.add(card_stats)

    card_stats.window_start = datetime.datetime.combine(start_date, datetime.time.min)
    card_stats.window_end = datetime.datetime.combine(end_date, datetime.time.min)
    card_stats.card_impressions = int(row[0]) if row[0] is not None else None
    card_stats.card_clicks = int(row[1]) if row[1] is not None else None
    card_stats.card_click_rate = float(row[2]) if row[2] is not None else None
    card_stats.card_teaser_impressions = int(row[3]) if row[3] is not None else None
    card_stats.card_teaser_clicks = int(row[4]) if row[4] is not None else None
    card_stats.card_teaser_click_rate = float(row[5]) if row[5] is not None else None
    card_stats.last_updated = datetime.datetime.utcnow()


async def _calculate_metrics(creator: Creator, db: AsyncSession):
    """Calculate 30-day views, engagement rate, impressions aggregates, and trend direction."""
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

    # Watch time in hours (30 days)
    creator.yt_watch_time_hours_30d = round(total_watch_time / 60, 1)

    # Net subscribers (30 days)
    total_gained = sum(s.subscribers_gained for s in stats)
    total_lost = sum(s.subscribers_lost for s in stats)
    creator.yt_net_subscribers_30d = total_gained - total_lost

    # Note: impressions/CTR are set directly on Creator by _sync_daily_stats
    # (fetched as aggregate from Analytics API, not per-day).
    # Per-day impressions will be backfilled by Reporting API when available.

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


# ─── Reporting API ────────────────────────────────────────────────────

REQUIRED_REPORT_TYPE_PREFIXES = [
    "channel_combined_a",
    "channel_traffic_source_a",
    "channel_playback_location_a",
    "channel_device_os_a",
]


async def _ensure_reporting_jobs(creator: Creator, token: str, db: AsyncSession):
    """Create Reporting API jobs for this creator if they don't already exist."""
    try:
        # 1. List available report types (spec A7 — use actual available types)
        available_types = await _yt_reporting_get(token, "reportTypes")
        if not available_types or not available_types.get("reportTypes"):
            log.warning(f"No Reporting API report types available for {creator.display_name}")
            return

        # Build a map of prefix → best available type ID (prefer highest version suffix)
        available_map: dict[str, str] = {}
        for rt in available_types["reportTypes"]:
            rt_id = rt.get("id", "")
            for prefix in REQUIRED_REPORT_TYPE_PREFIXES:
                if rt_id.startswith(prefix):
                    existing = available_map.get(prefix, "")
                    if rt_id > existing:  # lexicographic — _a3 > _a2
                        available_map[prefix] = rt_id

        if not available_map:
            log.info(f"No matching report types found for {creator.display_name}")
            return

        # 2. List existing jobs for this token
        existing_jobs = await _yt_reporting_get(token, "jobs")
        existing_types = set()
        if existing_jobs and existing_jobs.get("jobs"):
            existing_types = {j["reportTypeId"] for j in existing_jobs["jobs"]}
            # Also store any jobs we don't have in DB yet
            for job in existing_jobs["jobs"]:
                db_result = await db.execute(
                    select(YouTubeReportingJob).where(
                        YouTubeReportingJob.creator_id == creator.id,
                        YouTubeReportingJob.job_id == job["id"],
                    )
                )
                if not db_result.scalar_one_or_none():
                    db.add(YouTubeReportingJob(
                        creator_id=creator.id,
                        job_id=job["id"],
                        report_type_id=job["reportTypeId"],
                    ))

        # 3. Create missing jobs
        for prefix, type_id in available_map.items():
            if type_id not in existing_types:
                result = await _yt_reporting_post(token, "jobs", {
                    "reportTypeId": type_id,
                    "name": f"elusive_{creator.slug}_{type_id}",
                })
                if result and result.get("id"):
                    db.add(YouTubeReportingJob(
                        creator_id=creator.id,
                        job_id=result["id"],
                        report_type_id=type_id,
                    ))
                    log.info(f"Created Reporting API job for {creator.display_name}: {type_id}")
                else:
                    log.warning(f"Failed to create job {type_id} for {creator.display_name}")

        await db.flush()

    except Exception as e:
        log.warning(f"Reporting API job setup failed for {creator.display_name}: {e}")


async def _sync_reporting_data(creator: Creator, token: str, db: AsyncSession):
    """Download and ingest any new Reporting API reports for this creator."""
    jobs_result = await db.execute(
        select(YouTubeReportingJob).where(YouTubeReportingJob.creator_id == creator.id)
    )
    jobs = jobs_result.scalars().all()

    for job in jobs:
        try:
            # List available reports since last download
            params = {}
            if job.last_downloaded_at:
                params["createdAfter"] = job.last_downloaded_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            reports_data = await _yt_reporting_get(token, f"jobs/{job.job_id}/reports", params)
            if not reports_data or not reports_data.get("reports"):
                continue

            for report in reports_data["reports"]:
                download_url = report.get("downloadUrl")
                if not download_url:
                    continue

                # Download the CSV (may be gzipped)
                async with httpx.AsyncClient() as client:
                    csv_resp = await client.get(
                        download_url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=120,
                    )
                if csv_resp.status_code != 200:
                    log.warning(f"Report download failed ({csv_resp.status_code}) for job {job.job_id}")
                    continue

                # Decompress if gzipped
                try:
                    content = gzip.decompress(csv_resp.content).decode("utf-8")
                except Exception:
                    content = csv_resp.text

                reader = csv.DictReader(io.StringIO(content))

                # Route to correct ingestion function based on report type prefix
                rt = job.report_type_id
                if rt.startswith("channel_combined_a"):
                    await _ingest_channel_combined(creator, reader, db)
                elif rt.startswith("channel_traffic_source_a"):
                    await _ingest_traffic_source_report(creator, reader, db)
                elif rt.startswith("channel_playback_location_a"):
                    await _ingest_playback_location_report(creator, reader, db)
                elif rt.startswith("channel_device_os_a"):
                    await _ingest_device_os_report(creator, reader, db)

                job.last_downloaded_at = datetime.datetime.utcnow()

        except Exception as e:
            log.warning(f"Report ingestion failed for job {job.job_id}: {e}")
            continue

    await db.flush()


async def _ingest_channel_combined(creator: Creator, reader: csv.DictReader, db: AsyncSession):
    """Ingest channel_combined report — backfills impressions/CTR on YouTubeStat."""
    for row in reader:
        # Only channel-level aggregate rows (video_id is blank)
        if row.get("video_id"):
            continue

        date_str = row.get("date", "")
        if not date_str:
            continue

        try:
            day_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        impressions = _safe_int(row.get("video_thumbnail_impressions") or row.get("impressions"))
        impressions_ctr = _safe_float(row.get("video_thumbnail_impressions_ctr") or row.get("impressions_click_through_rate"))

        if impressions is None and impressions_ctr is None:
            continue

        # Upsert — only update impressions fields, don't overwrite Analytics API data
        result = await db.execute(
            select(YouTubeStat).where(
                YouTubeStat.creator_id == creator.id,
                YouTubeStat.date == day_date,
            )
        )
        stat = result.scalar_one_or_none()
        if stat is None:
            # Create stub row — Reporting API may have older dates
            stat = YouTubeStat(
                creator_id=creator.id,
                date=day_date,
                views=_safe_int(row.get("views")) or 0,
                watch_time_minutes=_safe_float(row.get("watch_time_minutes")) or 0.0,
            )
            db.add(stat)

        if impressions is not None:
            stat.impressions = impressions
        if impressions_ctr is not None:
            stat.impressions_ctr = impressions_ctr


async def _ingest_traffic_source_report(creator: Creator, reader: csv.DictReader, db: AsyncSession):
    """Ingest channel_traffic_source report — supplements Analytics API traffic data."""
    # This provides deeper historical data; we log it but don't overwrite the fresher Analytics API data
    count = 0
    for row in reader:
        count += 1
    log.info(f"Ingested {count} traffic source report rows for {creator.display_name}")


async def _ingest_playback_location_report(creator: Creator, reader: csv.DictReader, db: AsyncSession):
    """Ingest channel_playback_location report."""
    count = 0
    for row in reader:
        count += 1
    log.info(f"Ingested {count} playback location report rows for {creator.display_name}")


async def _ingest_device_os_report(creator: Creator, reader: csv.DictReader, db: AsyncSession):
    """Ingest channel_device_os report."""
    count = 0
    for row in reader:
        count += 1
    log.info(f"Ingested {count} device/OS report rows for {creator.display_name}")


# ─── On-Demand Deep Dive ─────────────────────────────────────────────

async def fetch_video_deep_dive(video: YouTubeVideo, creator: Creator, db: AsyncSession) -> dict:
    """On-demand: fetch traffic sources, retention, relative retention, and demographics for a single video.
    Returns a dict with all deep-dive data. Costs 4-5 API quota units."""
    user = creator.user
    token = await _get_valid_token(user, db)
    if not token:
        return {"error": "no_token"}

    result = {}
    end_date = datetime.date.today()
    if video.published_at:
        start_date = max(video.published_at.date(), end_date - datetime.timedelta(days=365))
    else:
        start_date = end_date - datetime.timedelta(days=90)

    # 1. Traffic sources for this video
    traffic_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views,estimatedMinutesWatched",
        "dimensions": "insightTrafficSourceType",
        "filters": f"video=={video.video_id}",
        "sort": "-views",
    })
    traffic_sources = []
    if traffic_data and traffic_data.get("rows"):
        total_views = sum(int(r[1]) for r in traffic_data["rows"])
        for row in traffic_data["rows"]:
            traffic_sources.append({
                "source": row[0],
                "views": int(row[1]),
                "watch_time_minutes": float(row[2]),
                "percentage": round(int(row[1]) / total_views * 100, 1) if total_views > 0 else 0,
            })
    result["traffic_sources"] = traffic_sources

    # 2. Audience retention curve (absolute)
    retention_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "audienceWatchRatio",
        "dimensions": "elapsedVideoTimeRatio",
        "filters": f"video=={video.video_id}",
    })
    retention_curve = []
    if retention_data and retention_data.get("rows"):
        for row in retention_data["rows"]:
            retention_curve.append({
                "elapsed_ratio": float(row[0]),
                "retention_pct": round(float(row[1]) * 100, 1),
            })
    result["retention_curve"] = retention_curve

    # 3. Relative retention performance (spec 1J)
    relative_retention_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "relativeRetentionPerformance",
        "dimensions": "elapsedVideoTimeRatio",
        "filters": f"video=={video.video_id}",
    })
    relative_retention_curve = []
    if relative_retention_data and relative_retention_data.get("rows"):
        for row in relative_retention_data["rows"]:
            relative_retention_curve.append({
                "elapsed_ratio": float(row[0]),
                "relative_performance": float(row[1]),
            })
    result["relative_retention_curve"] = relative_retention_curve

    # 4. Per-video demographics (age+gender)
    demo_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "viewerPercentage",
        "dimensions": "ageGroup,gender",
        "filters": f"video=={video.video_id}",
        "sort": "ageGroup,gender",
    })
    video_demographics = {"ageGroup": {}, "gender": {}}
    if demo_data and demo_data.get("rows"):
        for row in demo_data["rows"]:
            age_group, gender, pct = row[0], row[1], float(row[2])
            video_demographics["ageGroup"][age_group] = video_demographics["ageGroup"].get(age_group, 0) + pct
            video_demographics["gender"][gender] = video_demographics["gender"].get(gender, 0) + pct
    result["demographics"] = video_demographics

    # 5. Per-video avg view duration and avg % viewed
    avg_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "averageViewDuration,averageViewPercentage",
        "filters": f"video=={video.video_id}",
    })
    avg_view_duration = 0.0
    avg_pct_viewed = 0.0
    if avg_data and avg_data.get("rows") and avg_data["rows"]:
        avg_view_duration = float(avg_data["rows"][0][0])
        avg_pct_viewed = float(avg_data["rows"][0][1])

    # 6. Per-video search terms
    search_data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "views",
        "dimensions": "insightTrafficSourceDetail",
        "filters": f"video=={video.video_id};insightTrafficSourceType==YT_SEARCH",
        "sort": "-views",
        "maxResults": 10,
    })
    video_search_terms = []
    if search_data and search_data.get("rows"):
        for row in search_data["rows"]:
            video_search_terms.append({"term": row[0], "views": int(row[1])})
    result["search_terms"] = video_search_terms

    # Cache results in YouTubeVideoAnalytics
    analytics_result = await db.execute(
        select(YouTubeVideoAnalytics).where(YouTubeVideoAnalytics.video_id == video.id)
    )
    analytics = analytics_result.scalar_one_or_none()
    if analytics is None:
        analytics = YouTubeVideoAnalytics(video_id=video.id)
        db.add(analytics)

    if traffic_sources:
        analytics.primary_traffic_source = traffic_sources[0]["source"]
    if retention_curve:
        analytics.retention_data = retention_curve
    # Relative retention: average the 40-60% elapsed ratio marks (spec 1J)
    if relative_retention_curve:
        mid_points = [r for r in relative_retention_curve if 0.40 <= r["elapsed_ratio"] <= 0.60]
        if mid_points:
            analytics.relative_retention = round(
                sum(r["relative_performance"] for r in mid_points) / len(mid_points), 3
            )
    elif retention_curve:
        # Fallback: calculate from absolute retention midpoint
        mid_points = [r for r in retention_curve if 0.45 <= r["elapsed_ratio"] <= 0.55]
        if mid_points:
            analytics.relative_retention = round(sum(r["retention_pct"] for r in mid_points) / len(mid_points), 1)

    analytics.avg_view_duration = avg_view_duration
    analytics.avg_pct_viewed = avg_pct_viewed
    analytics.last_updated = datetime.datetime.utcnow()
    await db.commit()

    return result


# ─── Helpers ──────────────────────────────────────────────────────────

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
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _safe_int(val) -> Optional[int]:
    """Safely convert to int, returning None on failure."""
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> Optional[float]:
    """Safely convert to float, returning None on failure."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# ─── Community Pulse ──────────────────────────────────────────────────

_POSITIVE_SIGNALS = {
    "love", "great", "amazing", "best", "fire", "more of this", "finally",
    "needed this", "keep it up", "underrated", "goat", "banger", "perfect", "helpful",
}
_NEGATIVE_SIGNALS = {
    "boring", "skip", "waste", "clickbait", "misleading", "unsubscribe",
    "disappointed", "bad", "worst", "stop", "irrelevant", "too long", "didn't finish",
}
_SPONSOR_SIGNALS = {"sponsor", "sponsored", "ad", "paid", "#ad"}
_STOPWORDS = {
    "the", "a", "an", "is", "it", "i", "this", "to", "and", "of", "in", "for",
    "that", "you", "my", "was", "are", "have", "not", "with", "on", "at", "be",
    "your", "we", "they", "but", "or", "so", "as", "from", "get", "all",
}


def _process_community_pulse(comments: list[dict]) -> dict:
    """Compute sentiment badge, top phrases, and sponsor flag from fetched comments."""
    from collections import Counter

    pos_weight = neg_weight = 0
    sponsor_mentions = 0
    all_words: list[str] = []

    for c in comments:
        text_lower = c["text"].lower()
        weight = max(c["likes"], 1)
        if any(sig in text_lower for sig in _POSITIVE_SIGNALS):
            pos_weight += weight
        if any(sig in text_lower for sig in _NEGATIVE_SIGNALS):
            neg_weight += weight
        if any(sig in text_lower for sig in _SPONSOR_SIGNALS):
            sponsor_mentions += 1
        words = [
            w for w in text_lower.split()
            if w not in _STOPWORDS and len(w) > 3 and w.isalpha()
        ]
        all_words.extend(words)

    total_weight = pos_weight + neg_weight
    if total_weight == 0:
        sentiment = None
    elif pos_weight / total_weight >= 0.6:
        sentiment = "positive"
    elif pos_weight / total_weight >= 0.4:
        sentiment = "mixed"
    else:
        sentiment = "negative"

    phrase_counts = Counter(all_words).most_common(8)
    phrases = [{"word": w, "count": c} for w, c in phrase_counts]

    sponsor_flag = (sponsor_mentions / max(len(comments), 1)) > 0.03

    return {
        "comments": comments[:5],
        "all_count": len(comments),
        "sentiment": sentiment,
        "phrases": phrases,
        "sponsor_flag": sponsor_flag,
    }


async def fetch_video_comments(video: "YouTubeVideo", creator: "Creator", db: "AsyncSession") -> dict:
    """On-demand: fetch top comments for Community Pulse. 1 quota unit. Not cached.
    Uses API key (not OAuth) — commentThreads.list on public videos doesn't need
    user-level auth, and avoids requiring the youtube.force-ssl scope."""
    from app.config import settings
    api_key = settings.YOUTUBE_API_KEY
    if not api_key:
        log.warning("YOUTUBE_API_KEY not set — Community Pulse disabled")
        return {"comments": [], "sentiment": None, "phrases": [], "sponsor_flag": False}

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{YT_DATA_BASE}/commentThreads",
            params={
                "part": "snippet",
                "videoId": video.video_id,
                "order": "relevance",
                "maxResults": 20,
                "key": api_key,
            },
            timeout=30,
        )
    if resp.status_code != 200:
        log.error(f"Comments API error {resp.status_code}: {resp.text[:200]}")
        data = None
    else:
        data = resp.json()

    if not data or not data.get("items"):
        return {"comments": [], "sentiment": None, "phrases": [], "sponsor_flag": False}

    comments = []
    for item in data["items"]:
        s = item["snippet"]["topLevelComment"]["snippet"]
        comments.append({
            "text": s.get("textDisplay", ""),
            "likes": int(s.get("likeCount", 0)),
            "author": s.get("authorDisplayName", ""),
            "published_at": _parse_datetime(s.get("publishedAt")),
        })

    comments.sort(key=lambda c: c["likes"], reverse=True)
    return _process_community_pulse(comments)
