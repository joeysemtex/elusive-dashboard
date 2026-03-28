"""Pipeline API endpoints for Cowork/Claude Code integration."""
import datetime
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic, YouTubeVideoAnalytics, YouTubeTrafficSource

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

    # All videos (no limit — intake needs full history for conservative avg)
    vids_result = await db.execute(
        select(YouTubeVideo)
        .options(selectinload(YouTubeVideo.analytics))
        .where(YouTubeVideo.creator_id == creator_id)
        .order_by(YouTubeVideo.published_at.desc())
    )
    videos = vids_result.scalars().all()

    # Daily stats (30 days) — includes subscribers_gained/lost
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator_id)
        .order_by(YouTubeStat.date.desc())
        .limit(30)
    )
    daily_stats = stats_result.scalars().all()

    # Demographics — includes subscribedStatus for returning viewer proxy
    demo_result = await db.execute(
        select(YouTubeDemographic)
        .where(YouTubeDemographic.creator_id == creator_id)
    )
    demographics = demo_result.scalars().all()

    # Channel-level traffic sources (video_id IS NULL = channel aggregate)
    traffic_result = await db.execute(
        select(YouTubeTrafficSource)
        .where(
            YouTubeTrafficSource.creator_id == creator_id,
            YouTubeTrafficSource.video_id == None,
        )
        .order_by(YouTubeTrafficSource.views.desc())
    )
    traffic_sources = traffic_result.scalars().all()

    # Derived: net subscribers over window
    subs_gained = sum(s.subscribers_gained or 0 for s in daily_stats)
    subs_lost   = sum(s.subscribers_lost  or 0 for s in daily_stats)
    net_subs_30d = subs_gained - subs_lost

    # Derived: returning viewer % from subscribedStatus demographic
    subscribed_pct = next(
        (d.percentage for d in demographics
         if d.dimension == "subscribedStatus" and "subscribed" in (d.value or "").lower()
         and "not" not in (d.value or "").lower()),
        None
    )

    # Total traffic views (for % calculation)
    total_traffic_views = sum(t.views or 0 for t in traffic_sources) or 1

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
            "last_yt_sync": creator.last_yt_sync.isoformat() if creator.last_yt_sync else None,
            # Derived fields
            "yt_net_subscribers_30d": net_subs_30d,
            "returning_viewer_pct": round(subscribed_pct, 1) if subscribed_pct is not None else None,
        },
        "videos": [
            {
                "video_id": v.video_id,
                "title": v.title,
                "thumbnail_url": v.thumbnail_url,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "duration_seconds": v.duration_seconds,
                "views": v.views,
                "likes": v.likes,
                "comments": v.comments,
                "engagement_rate": round(v.engagement_rate, 2),
                "format": "short" if (v.duration_seconds or 0) < 60 else "long_form",
                # Per-video deep analytics (None if not yet collected)
                "avg_view_duration_seconds": round(v.analytics.avg_view_duration, 1) if v.analytics else None,
                "avg_pct_viewed": round(v.analytics.avg_pct_viewed, 1) if v.analytics else None,
            }
            for v in videos
        ],
        "daily_stats": [
            {
                "date": s.date.strftime("%Y-%m-%d"),
                "views": s.views,
                "watch_time_minutes": round(s.watch_time_minutes, 1),
                "likes": s.likes,
                "comments": s.comments,
                "subscribers_gained": s.subscribers_gained or 0,
                "subscribers_lost": s.subscribers_lost or 0,
            }
            for s in reversed(daily_stats)
        ],
        "demographics": {
            dim: [
                {"value": d.value, "percentage": round(d.percentage, 1)}
                for d in demographics if d.dimension == dim
            ]
            for dim in ["ageGroup", "gender", "country", "deviceType", "subscribedStatus"]
        },
        "traffic_sources": [
            {
                "source": t.source_type,
                "views": t.views,
                "watch_time_minutes": round(t.watch_time_minutes, 1),
                "pct": round((t.views or 0) / total_traffic_views * 100, 1),
            }
            for t in traffic_sources
        ] if traffic_sources else [],
        "long_form": {
            "count": len([v for v in videos if (v.duration_seconds or 0) >= 60]),
            "videos": [
                {
                    "video_id": v.video_id, "title": v.title, "views": v.views,
                    "published_at": v.published_at.isoformat() if v.published_at else None,
                    "engagement_rate": round(v.engagement_rate, 2),
                    "duration_seconds": v.duration_seconds,
                    "avg_pct_viewed": round(v.analytics.avg_pct_viewed, 1) if v.analytics else None,
                }
                for v in videos if (v.duration_seconds or 0) >= 60
            ],
        },
        "shorts": {
            "count": len([v for v in videos if (v.duration_seconds or 0) < 60]),
            "videos": [
                {
                    "video_id": v.video_id, "title": v.title, "views": v.views,
                    "published_at": v.published_at.isoformat() if v.published_at else None,
                    "engagement_rate": round(v.engagement_rate, 2),
                    "duration_seconds": v.duration_seconds,
                }
                for v in videos if (v.duration_seconds or 0) < 60
            ],
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

    # Recent videos for top performers
    vids_result = await db.execute(
        select(YouTubeVideo)
        .where(YouTubeVideo.creator_id == creator_id)
        .order_by(YouTubeVideo.views.desc())
        .limit(5)
    )
    top_videos = vids_result.scalars().all()

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
        "top_videos": [
            {
                "title": v.title,
                "views": v.views,
                "likes": v.likes,
                "engagement_rate": round(v.engagement_rate, 2),
                "format": "short" if (v.duration_seconds or 0) < 60 else "long_form",
                "url": f"https://youtube.com/watch?v={v.video_id}",
            }
            for v in top_videos
        ],
        "instagram": {
            "username": creator.instagram_username,
            "followers": creator.ig_followers,
            "reach_30d": creator.ig_reach_30d,
            "engagement_rate": round(creator.ig_engagement_rate, 2) if creator.ig_engagement_rate else None,
            "last_sync": creator.last_ig_sync.isoformat() if creator.last_ig_sync else None,
        },
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "source": "elusive-dashboard",
    }


@router.delete("/creators/{creator_id}", status_code=204)
async def delete_creator(
    creator_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_verify_api_key),
):
    """Delete a creator and all associated data."""
    result = await db.execute(
        select(Creator).where(Creator.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    user_id = creator.user_id

    # Delete related records first (no DB-level cascade configured)
    # Video analytics (FK to youtube_videos.id) must be deleted before videos
    video_ids_subq = select(YouTubeVideo.id).where(YouTubeVideo.creator_id == creator_id)
    await db.execute(delete(YouTubeVideoAnalytics).where(YouTubeVideoAnalytics.video_id.in_(video_ids_subq)))
    await db.execute(delete(YouTubeTrafficSource).where(YouTubeTrafficSource.creator_id == creator_id))
    await db.execute(delete(YouTubeDemographic).where(YouTubeDemographic.creator_id == creator_id))
    await db.execute(delete(YouTubeVideo).where(YouTubeVideo.creator_id == creator_id))
    await db.execute(delete(YouTubeStat).where(YouTubeStat.creator_id == creator_id))

    # Delete the creator row, then the associated user
    await db.delete(creator)
    await db.flush()

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        await db.delete(user)

    await db.commit()
    return Response(status_code=204)
