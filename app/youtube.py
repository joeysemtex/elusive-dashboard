"""YouTube Data API v3 + Analytics API client."""
import datetime
import logging
from typing import Optional

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_token, encrypt_token
from app.config import settings
from app.models import Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic, YouTubeVideoAnalytics, YouTubeTrafficSource

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

        # 5. Subscribed status
        await _sync_subscribed_status(creator, token, db)

        # 6. Traffic sources
        await _sync_traffic_sources(creator, token, db)

        # 7. Per-video analytics (batch)
        await _sync_video_analytics_batch(creator, token, db)

        # 8. Calculate engagement rate and trend
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

    # Fallback to brand/managed channels if mine has no videos
    channel = None
    if data and data.get("items"):
        channel = data["items"][0]
    if not channel or int(channel.get("statistics", {}).get("videoCount", 0)) == 0:
        managed = await _yt_get(token, "channels", {"part": "snippet,statistics", "managedByMe": "true"})
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
    """Pull the 10 most recent videos with stats."""
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
    """Pull daily analytics for the last 60 days."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=60)

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

    # ageGroup and gender must be queried as combined dimension with viewerPercentage
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

    # country and deviceType use views metric, converted to percentages
    for dimension in ["country", "deviceType"]:
        data = await _yt_analytics_get(token, {
            "ids": "channel==MINE",
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "metrics": "views",
            "dimensions": dimension,
            "sort": "-views",
            "maxResults": 10,
        })
        if not data or not data.get("rows"):
            continue

        total = sum(float(r[1]) for r in data["rows"])
        for row in data["rows"]:
            pct = (float(row[1]) / total * 100) if total > 0 else 0
            db.add(YouTubeDemographic(
                creator_id=creator.id, dimension=dimension,
                value=row[0], percentage=round(pct, 1),
            ))


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

    # Watch time in hours (30 days)
    creator.yt_watch_time_hours_30d = round(total_watch_time / 60, 1)

    # Net subscribers (30 days)
    total_gained = sum(s.subscribers_gained for s in stats)
    total_lost = sum(s.subscribers_lost for s in stats)
    creator.yt_net_subscribers_30d = total_gained - total_lost

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


async def _sync_subscribed_status(creator: Creator, token: str, db: AsyncSession):
    """Pull subscriber vs non-subscriber view breakdown."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=90)

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


async def _sync_video_analytics_batch(creator: Creator, token: str, db: AsyncSession):
    """Batch-sync avg view duration for recent videos via Analytics API."""
    if not creator.youtube_channel_id:
        return

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=30)

    data = await _yt_analytics_get(token, {
        "ids": "channel==MINE",
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "metrics": "averageViewDuration,averageViewPercentage",
        "dimensions": "video",
        "sort": "-views",
        "maxResults": 50,
    })
    if not data or not data.get("rows"):
        return

    for row in data["rows"]:
        yt_video_id = row[0]
        avg_duration = float(row[1])
        avg_pct = float(row[2])

        # Find the YouTubeVideo record by video_id string
        result = await db.execute(
            select(YouTubeVideo).where(YouTubeVideo.video_id == yt_video_id)
        )
        video = result.scalar_one_or_none()
        if not video:
            continue

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
        analytics.last_updated = datetime.datetime.utcnow()


async def fetch_video_deep_dive(video: YouTubeVideo, creator: Creator, db: AsyncSession) -> dict:
    """On-demand: fetch traffic sources, retention, and demographics for a single video.
    Returns a dict with all deep-dive data. Costs 3 API quota units."""
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

    # 2. Audience retention curve
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

    # 3. Per-video demographics (age+gender)
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
        mid_points = [r for r in retention_curve if 0.45 <= r["elapsed_ratio"] <= 0.55]
        if mid_points:
            analytics.relative_retention = round(sum(r["retention_pct"] for r in mid_points) / len(mid_points), 1)
    analytics.last_updated = datetime.datetime.utcnow()
    await db.commit()

    return result


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


# --- Community Pulse ---

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
    """On-demand: fetch top comments for Community Pulse. 1 quota unit. Not cached."""
    user = creator.user
    token = await _get_valid_token(user, db)
    if not token:
        return {"comments": [], "sentiment": None, "phrases": [], "sponsor_flag": False}

    data = await _yt_get(token, "commentThreads", {
        "part": "snippet",
        "videoId": video.video_id,
        "order": "relevance",
        "maxResults": 20,
    })

    # commentThreads returns 403 commentsDisabled if comments are off — _yt_get returns None
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

    # Sort by likes descending (relevance order already surfaces engaged comments)
    comments.sort(key=lambda c: c["likes"], reverse=True)
    return _process_community_pulse(comments)
