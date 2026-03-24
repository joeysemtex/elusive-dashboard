"""Pipeline API endpoints for Cowork/Claude Code integration."""
import datetime
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import Creator, YouTubeStat, YouTubeVideo, YouTubeDemographic

router = APIRouter(prefix="/api", tags=["pipeline"])


def _verify_api_key(x_api_key: str = Header(None)):
    """Validate pipeline API key."""
    if not x_api_key or x_api_key != settings.PIPELINE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@router.get("/creators")
async def list_creators(
    db: AsyncSession = Depends(get_db),
    _=Depends(_verify_api_key),
):
    """List all creators with summary stats."""
    result = await db.execute(
        select(Creator).where(Creator.is_active == True).order_by(Creator.display_name)
    )
    creators = result.scalars().all()

    return {
        "creators": [
            {
                "id": c.id,
                "name": c.display_name,
                "slug": c.slug,
                "avatar_url": c.avatar_url,
                "youtube": {
                    "channel_id": c.youtube_channel_id,
                    "subscribers": c.yt_subscribers,
                    "total_views": c.yt_total_views,
                    "video_count": c.yt_video_count,
                    "views_30d": c.yt_30d_views,
                    "engagement_rate": round(c.yt_engagement_rate, 2),
                    "avg_view_duration_seconds": round(c.yt_avg_view_duration, 1),
                },
                "instagram": {
                    "followers": c.ig_followers,
                    "reach_30d": c.ig_reach_30d,
                    "engagement_rate": round(c.ig_engagement_rate, 2),
                },
                "trend": c.trend_direction,
                "last_yt_sync": c.last_yt_sync.isoformat() if c.last_yt_sync else None,
            }
            for c in creators
        ],
        "count": len(creators),
        "generated_at": datetime.datetime.utcnow().isoformat(),
    }


@router.get("/creators/{creator_id}/youtube")
async def get_creator_youtube(
    creator_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_verify_api_key),
):
    """Full YouTube stats for one creator."""
    result = await db.execute(
        select(Creator).where(Creator.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    # All videos
    vids_result = await db.execute(
        select(YouTubeVideo)
        .where(YouTubeVideo.creator_id == creator_id)
        .order_by(YouTubeVideo.published_at.desc())
    )
    all_videos = vids_result.scalars().all()

    # Split by format
    longform_videos = [v for v in all_videos if v.duration_seconds >= 60]
    shorts_videos = [v for v in all_videos if v.duration_seconds < 60]

    def _format_video(v):
        return {
            "video_id": v.video_id,
            "title": v.title,
            "thumbnail_url": v.thumbnail_url,
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "duration_seconds": v.duration_seconds,
            "views": v.views,
            "likes": v.likes,
            "comments": v.comments,
            "engagement_rate": round(v.engagement_rate, 2),
        }

    def _format_metrics(vids):
        if not vids:
            return {"avg_views": 0, "avg_engagement": 0.0, "avg_duration": 0, "count": 0}
        return {
            "avg_views": int(sum(v.views for v in vids) / len(vids)),
            "avg_engagement": round(sum(v.engagement_rate for v in vids) / len(vids), 2),
            "avg_duration": int(sum(v.duration_seconds for v in vids) / len(vids)),
            "count": len(vids),
        }

    # Daily stats (30 days)
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator_id)
        .order_by(YouTubeStat.date.desc())
        .limit(30)
    )
    daily_stats = stats_result.scalars().all()

    # Demographics (now includes deviceType)
    demo_result = await db.execute(
        select(YouTubeDemographic)
        .where(YouTubeDemographic.creator_id == creator_id)
    )
    demographics = demo_result.scalars().all()

    return {
        "creator": {
            "id": creator.id,
            "name": creator.display_name,
            "channel_id": creator.youtube_channel_id,
            "channel_title": creator.youtube_channel_title,
            "subscribers": creator.yt_subscribers,
            "total_views": creator.yt_total_views,
            "video_count": creator.yt_video_count,
            "views_30d": creator.yt_30d_views,
            "engagement_rate": round(creator.yt_engagement_rate, 2),
            "avg_view_duration_seconds": round(creator.yt_avg_view_duration, 1),
            "trend": creator.trend_direction,
        },
        "longform": {
            "metrics": _format_metrics(longform_videos),
            "videos": [_format_video(v) for v in longform_videos[:10]],
        },
        "shorts": {
            "metrics": _format_metrics(shorts_videos),
            "videos": [_format_video(v) for v in shorts_videos[:10]],
        },
        "daily_stats": [
            {
                "date": s.date.strftime("%Y-%m-%d"),
                "views": s.views,
                "watch_time_minutes": round(s.watch_time_minutes, 1),
                "likes": s.likes,
                "comments": s.comments,
            }
            for s in reversed(daily_stats)
        ],
        "demographics": {
            dim: [
                {"value": d.value, "percentage": round(d.percentage, 1)}
                for d in demographics if d.dimension == dim
            ]
            for dim in ["ageGroup", "gender", "country", "deviceType"]
        },
    }


@router.get("/creators/{creator_id}/export")
async def export_creator_pitch(
    creator_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_verify_api_key),
):
    """Pitch-ready JSON matching data-intake schema."""
    result = await db.execute(
        select(Creator).where(Creator.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    # Daily stats for averages
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator_id)
        .order_by(YouTubeStat.date.desc())
        .limit(30)
    )
    daily_stats = stats_result.scalars().all()

    # All videos for format-separated top performers
    vids_result = await db.execute(
        select(YouTubeVideo)
        .where(YouTubeVideo.creator_id == creator_id)
        .order_by(YouTubeVideo.views.desc())
    )
    all_videos = vids_result.scalars().all()

    longform = [v for v in all_videos if v.duration_seconds >= 60]
    shorts = [v for v in all_videos if v.duration_seconds < 60]

    def _top_vids(vids, limit=5):
        return [
            {
                "title": v.title,
                "views": v.views,
                "likes": v.likes,
                "duration_seconds": v.duration_seconds,
                "engagement_rate": round(v.engagement_rate, 2),
                "url": f"https://youtube.com/watch?v={v.video_id}",
            }
            for v in vids[:limit]
        ]

    avg_views = sum(s.views for s in daily_stats) / len(daily_stats) if daily_stats else 0

    return {
        "creator_name": creator.display_name,
        "platform": "youtube",
        "channel_url": creator.youtube_channel_url,
        "subscribers": creator.yt_subscribers,
        "total_views": creator.yt_total_views,
        "views_30d": creator.yt_30d_views,
        "avg_daily_views": round(avg_views),
        "engagement_rate": round(creator.yt_engagement_rate, 2),
        "avg_view_duration_seconds": round(creator.yt_avg_view_duration, 1),
        "trend": creator.trend_direction,
        "top_videos": _top_vids(all_videos[:5]),
        "longform": {
            "count": len(longform),
            "avg_views": int(sum(v.views for v in longform) / len(longform)) if longform else 0,
            "avg_engagement": round(sum(v.engagement_rate for v in longform) / len(longform), 2) if longform else 0,
            "top_videos": _top_vids(longform),
        },
        "shorts": {
            "count": len(shorts),
            "avg_views": int(sum(v.views for v in shorts) / len(shorts)) if shorts else 0,
            "avg_engagement": round(sum(v.engagement_rate for v in shorts) / len(shorts), 2) if shorts else 0,
            "top_videos": _top_vids(shorts),
        },
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "source": "elusive-dashboard",
    }
