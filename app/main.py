"""Elusive Analytics Dashboard — FastAPI application."""
import asyncio
import datetime
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, init_db
from app.models import Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic, YouTubeVideoAnalytics, YouTubeTrafficSource, YouTubeSearchTerm, YouTubeCardStats, YouTubeReportingJob
from app.auth import handle_google_login, handle_google_callback
from app.api import router as api_router
from app.scheduler import start_scheduler, stop_scheduler
from app.youtube import sync_creator_youtube, fetch_video_deep_dive, fetch_video_comments

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("elusive")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    await init_db()
    start_scheduler()
    log.info("Elusive Dashboard started")
    yield
    stop_scheduler()
    log.info("Elusive Dashboard stopped")


app = FastAPI(title="Elusive Analytics Dashboard", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(api_router)

templates = Jinja2Templates(directory="templates")


# --- Template filters ---

def format_number(value):
    """Format large numbers: 1234567 -> 1.23M, 12345 -> 12.3K."""
    if value is None:
        return "0"
    value = int(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_duration(seconds):
    """Format seconds to human-readable duration."""
    if not seconds:
        return "0:00"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    if minutes >= 60:
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}m"
    return f"{minutes}:{secs:02d}"


def format_percent(value):
    """Format percentage."""
    if value is None:
        return "0%"
    return f"{value:.1f}%"


def timeago(dt):
    """Human-readable time ago."""
    if not dt:
        return "never"
    now = datetime.datetime.utcnow()
    diff = now - dt
    if diff.days > 0:
        return f"{diff.days}d ago"
    hours = diff.seconds // 3600
    if hours > 0:
        return f"{hours}h ago"
    minutes = diff.seconds // 60
    return f"{minutes}m ago"


def strftime_filter(dt, fmt="%b %d"):
    """Format datetime with strftime."""
    if not dt:
        return ""
    return dt.strftime(fmt)


templates.env.filters["format_number"] = format_number
templates.env.filters["format_duration"] = format_duration
templates.env.filters["format_percent"] = format_percent
templates.env.filters["timeago"] = timeago
templates.env.filters["strftime"] = strftime_filter


# --- Auth helpers ---

async def get_current_user(request: Request, db: AsyncSession) -> User | None:
    """Get the logged-in user from session."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def require_auth(request: Request):
    """Redirect to login if not authenticated."""
    if not request.session.get("user_id"):
        return RedirectResponse("/login", status_code=302)
    return None


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    redirect = require_auth(request)
    if redirect:
        return redirect

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    if user.role in ("admin", "viewer"):
        return RedirectResponse("/dashboard")
    else:
        # Creator goes to their own dashboard
        result = await db.execute(
            select(Creator).where(Creator.user_id == user.id)
        )
        creator = result.scalar_one_or_none()
        if creator:
            return RedirectResponse(f"/creator/{creator.slug}")
        # No creator profile — send to login instead of /dashboard to avoid redirect loop
        request.session.clear()
        return RedirectResponse("/login?error=no_profile")


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request):
    return templates.TemplateResponse(request, "privacy.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.get("/auth/google")
async def google_login(request: Request):
    return await handle_google_login(request)


@app.get("/auth/google/callback")
async def google_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user = await handle_google_callback(request, db)
        request.session["user_id"] = user.id
        request.session["user_role"] = user.role
    except Exception as e:
        log.error(f"OAuth callback error: {e}")
        return RedirectResponse("/login?error=auth_failed", status_code=302)

    # YouTube sync is best-effort — never blocks login
    if user.role == "creator":
        try:
            result = await db.execute(
                select(Creator).where(Creator.user_id == user.id)
            )
            creator = result.scalar_one_or_none()
            if creator and not creator.last_yt_sync:
                await db.refresh(creator, ["user"])
                await sync_creator_youtube(creator, db)
        except Exception as e:
            log.warning(f"Initial YouTube sync failed for {user.name}: {e}")

    return RedirectResponse("/", status_code=302)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def agency_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    redirect = require_auth(request)
    if redirect:
        return redirect

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    # Admin and viewer can see agency dashboard
    if user.role not in ("admin", "viewer"):
        return RedirectResponse("/")

    result = await db.execute(
        select(Creator).where(Creator.is_active == True).order_by(Creator.display_name)
    )
    creators = result.scalars().all()

    # Get sparkline data for each creator (last 14 days of views)
    sparklines = {}
    for creator in creators:
        stats_result = await db.execute(
            select(YouTubeStat)
            .where(YouTubeStat.creator_id == creator.id)
            .order_by(YouTubeStat.date.desc())
            .limit(14)
        )
        stats = stats_result.scalars().all()
        sparklines[creator.id] = [s.views for s in reversed(stats)]

    return templates.TemplateResponse(request, "agency.html", {
        "user": user,
        "creators": creators,
        "sparklines": sparklines,
        "page_title": "Agency Dashboard",
        "now_utc": datetime.datetime.utcnow(),
    })


# --- Helpers for tab endpoints ---

def _compute_format_metrics(videos):
    """Compute aggregate metrics for a list of videos."""
    if not videos:
        return {"count": 0, "avg_views": 0, "avg_engagement": 0.0, "avg_duration": 0}
    count = len(videos)
    return {
        "count": count,
        "avg_views": int(sum(v.views or 0 for v in videos) / count),
        "avg_engagement": round(sum(v.engagement_rate or 0 for v in videos) / count, 2),
        "avg_duration": int(sum(v.duration_seconds or 0 for v in videos) / count),
    }


async def _get_creator_for_request(slug: str, request: Request, db: AsyncSession):
    """Auth check + creator lookup for HTMX partial endpoints."""
    if not request.session.get("user_id"):
        raise HTTPException(status_code=401)
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401)
    result = await db.execute(select(Creator).where(Creator.slug == slug))
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404)
    if user.role not in ("admin", "viewer") and creator.user_id != user.id:
        raise HTTPException(status_code=403)
    return user, creator


async def _get_all_creator_videos(creator_id: int, db: AsyncSession):
    """Fetch all videos for a creator, split into long_form and shorts."""
    vids_result = await db.execute(
        select(YouTubeVideo)
        .where(YouTubeVideo.creator_id == creator_id)
        .order_by(YouTubeVideo.published_at.desc())
    )
    all_videos = vids_result.scalars().all()
    long_form = [v for v in all_videos if (v.duration_seconds or 0) >= 60]
    shorts = [v for v in all_videos if (v.duration_seconds or 0) < 60]
    return long_form, shorts


TRAFFIC_SOURCE_LABELS = {
    "YT_SEARCH": "Search",
    "SUGGESTED": "Suggested",
    "RELATED_VIDEO": "Suggested",
    "BROWSE": "Browse Features",
    "EXT_URL": "External",
    "NO_LINK_EMBEDDED": "Embedded",
    "SUBSCRIBER": "Subscribers",
    "NOTIFICATION": "Notifications",
    "YT_CHANNEL": "Channel Page",
    "PLAYLIST": "Playlists",
    "ADVERTISING": "Advertising",
    "SHORTS": "Shorts Feed",
    "END_SCREEN": "End Screens",
    "ANNOTATION": "Cards",
    "CAMPAIGN_CARD": "Campaign Cards",
    "NO_LINK_OTHER": "Other",
}


def _format_traffic_source(source_type: str) -> str:
    return TRAFFIC_SOURCE_LABELS.get(source_type, source_type.replace("_", " ").title())


async def _get_period_comparison(creator_id: int, db: AsyncSession) -> dict:
    """Compute current 30d vs prior 30d for period comparison badges."""
    result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator_id)
        .order_by(YouTubeStat.date.desc())
        .limit(60)
    )
    stats = result.scalars().all()

    current = stats[:30]
    prior = stats[30:60]

    def _compare(current_vals, prior_vals):
        c = sum(current_vals)
        p = sum(prior_vals)
        if p == 0:
            return {"current": c, "prior": p, "change_pct": 0, "direction": "flat"}
        change = round((c - p) / p * 100, 1)
        direction = "up" if change > 0 else "down" if change < 0 else "flat"
        return {"current": c, "prior": p, "change_pct": change, "direction": direction}

    if len(prior) < 7:
        return {"has_comparison": False}

    return {
        "has_comparison": True,
        "views": _compare([s.views for s in current], [s.views for s in prior]),
        "watch_time": _compare([s.watch_time_minutes for s in current], [s.watch_time_minutes for s in prior]),
        "subscribers": _compare(
            [s.subscribers_gained - s.subscribers_lost for s in current],
            [s.subscribers_gained - s.subscribers_lost for s in prior],
        ),
        "engagement": _compare([s.likes + s.comments for s in current], [s.likes + s.comments for s in prior]),
    }


async def _get_traffic_data(creator_id: int, db: AsyncSession) -> dict:
    """Get traffic source data for the Traffic tab."""
    agg_result = await db.execute(
        select(YouTubeTrafficSource).where(
            YouTubeTrafficSource.creator_id == creator_id,
            YouTubeTrafficSource.video_id == None,
            YouTubeTrafficSource.date == None,
        ).order_by(YouTubeTrafficSource.views.desc())
    )
    aggregate = agg_result.scalars().all()

    daily_result = await db.execute(
        select(YouTubeTrafficSource).where(
            YouTubeTrafficSource.creator_id == creator_id,
            YouTubeTrafficSource.video_id == None,
            YouTubeTrafficSource.date != None,
        ).order_by(YouTubeTrafficSource.date.asc())
    )
    daily = daily_result.scalars().all()

    total_views = sum(t.views for t in aggregate)
    search_views = sum(t.views for t in aggregate if t.source_type in ("YT_SEARCH",))
    suggested_views = sum(t.views for t in aggregate if t.source_type in ("SUGGESTED", "RELATED_VIDEO"))
    browse_views = sum(t.views for t in aggregate if t.source_type in ("BROWSE", "YT_CHANNEL"))
    external_views = sum(t.views for t in aggregate if t.source_type in ("EXT_URL", "NO_LINK_EMBEDDED"))

    top_source = _format_traffic_source(aggregate[0].source_type) if aggregate else "N/A"

    # Convert to dicts for template serialization
    agg_dicts = [{"source": _format_traffic_source(t.source_type), "views": t.views, "watch_time": round(t.watch_time_minutes, 1)} for t in aggregate]
    daily_dicts = [{"date": t.date.strftime("%b %d"), "source": _format_traffic_source(t.source_type), "views": t.views} for t in daily]

    return {
        "aggregate": agg_dicts,
        "daily": daily_dicts,
        "total_views": total_views,
        "top_source": top_source,
        "search_pct": round(search_views / total_views * 100, 1) if total_views > 0 else 0,
        "algorithmic_pct": round((suggested_views + browse_views) / total_views * 100, 1) if total_views > 0 else 0,
        "external_pct": round(external_views / total_views * 100, 1) if total_views > 0 else 0,
    }


def _get_audience_metrics(demo_data: dict) -> dict:
    """Derive top-line audience metrics from demographics data."""
    def _top(dimension):
        items = demo_data.get(dimension, [])
        if not items:
            return "N/A"
        top = max(items, key=lambda x: x["percentage"])
        return f"{top['value']} ({top['percentage']:.0f}%)"

    return {
        "top_age": _top("ageGroup"),
        "top_country": _top("country"),
        "primary_device": _top("deviceType"),
        "gender_majority": _top("gender"),
    }


@app.get("/creator/{slug}", response_class=HTMLResponse)
async def creator_dashboard(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    redirect = require_auth(request)
    if redirect:
        return redirect

    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")

    result = await db.execute(
        select(Creator).where(Creator.slug == slug)
    )
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    # Creators can only see their own dashboard (admin + viewer can see all)
    if user.role not in ("admin", "viewer"):
        if not creator.user_id == user.id:
            raise HTTPException(status_code=403, detail="Access denied")

    # Daily stats (60 days for period comparison, most recent 30 for chart)
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator.id)
        .order_by(YouTubeStat.date.desc())
        .limit(60)
    )
    all_stats = list(reversed(stats_result.scalars().all()))
    daily_stats = all_stats[-30:] if len(all_stats) > 30 else all_stats

    # Period comparison
    period_comparison = await _get_period_comparison(creator.id, db)

    # Format-specific metrics for Overview split cards
    long_form, shorts = await _get_all_creator_videos(creator.id, db)
    lf_metrics = _compute_format_metrics(long_form)
    shorts_metrics = _compute_format_metrics(shorts)

    return templates.TemplateResponse(request, "creator.html", {
        "user": user,
        "creator": creator,
        "daily_stats": daily_stats,
        "period_comparison": period_comparison,
        "lf_metrics": lf_metrics,
        "shorts_metrics": shorts_metrics,
        "page_title": creator.display_name,
    })


# --- HTMX tab partials ---

@app.get("/creator/{slug}/tab/overview", response_class=HTMLResponse)
async def tab_overview(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user, creator = await _get_creator_for_request(slug, request, db)
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator.id)
        .order_by(YouTubeStat.date.desc())
        .limit(60)
    )
    all_stats = list(reversed(stats_result.scalars().all()))
    daily_stats = all_stats[-30:] if len(all_stats) > 30 else all_stats
    period_comparison = await _get_period_comparison(creator.id, db)

    # Format-specific metrics for split cards
    long_form, shorts = await _get_all_creator_videos(creator.id, db)
    lf_metrics = _compute_format_metrics(long_form)
    shorts_metrics = _compute_format_metrics(shorts)

    return templates.TemplateResponse(request, "partials/tab_overview.html", {
        "creator": creator,
        "daily_stats": daily_stats,
        "period_comparison": period_comparison,
        "lf_metrics": lf_metrics,
        "shorts_metrics": shorts_metrics,
    })


@app.get("/creator/{slug}/tab/content", response_class=HTMLResponse)
async def tab_content(slug: str, request: Request, db: AsyncSession = Depends(get_db),
                      format: str = "all"):
    user, creator = await _get_creator_for_request(slug, request, db)
    long_form, shorts = await _get_all_creator_videos(creator.id, db)

    # Filter to last 30 days for performance tables
    thirty_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=30)
    lf_30d = [v for v in long_form if v.published_at and v.published_at >= thirty_days_ago]
    shorts_30d = [v for v in shorts if v.published_at and v.published_at >= thirty_days_ago]

    if format == "longform":
        videos_30d = lf_30d
    elif format == "shorts":
        videos_30d = shorts_30d
    else:
        videos_30d = sorted(lf_30d + shorts_30d, key=lambda v: v.views or 0, reverse=True)

    # Filter out very recent videos (< 7 days) from underperformers
    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    mature_videos = [v for v in videos_30d if v.published_at and v.published_at < seven_days_ago]

    top_10 = videos_30d[:10]
    bottom_10 = sorted(mature_videos, key=lambda v: v.views or 0)[:10] if len(mature_videos) > 10 else []

    return templates.TemplateResponse(request, "partials/tab_content.html", {
        "creator": creator,
        "top_videos": top_10,
        "bottom_videos": bottom_10,
        "content_metrics": _compute_format_metrics(videos_30d),
        "active_format": format,
        "total_long_form": len(lf_30d),
        "total_shorts": len(shorts_30d),
        "total_all": len(lf_30d) + len(shorts_30d),
    })


@app.get("/creator/{slug}/tab/audience", response_class=HTMLResponse)
async def tab_audience(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user, creator = await _get_creator_for_request(slug, request, db)
    demo_result = await db.execute(
        select(YouTubeDemographic)
        .where(YouTubeDemographic.creator_id == creator.id)
    )
    demographics = demo_result.scalars().all()
    demo_data = {}
    for d in demographics:
        demo_data.setdefault(d.dimension, []).append({"value": d.value, "percentage": d.percentage, "avg_view_duration": d.avg_view_duration})

    return templates.TemplateResponse(request, "partials/tab_audience.html", {
        "creator": creator,
        "demographics": demo_data,
        "audience_metrics": _get_audience_metrics(demo_data),
    })


@app.get("/creator/{slug}/tab/traffic", response_class=HTMLResponse)
async def tab_traffic(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user, creator = await _get_creator_for_request(slug, request, db)
    traffic = await _get_traffic_data(creator.id, db)

    # Search keywords
    search_result = await db.execute(
        select(YouTubeSearchTerm)
        .where(YouTubeSearchTerm.creator_id == creator.id)
        .order_by(YouTubeSearchTerm.views.desc())
        .limit(25)
    )
    search_terms = search_result.scalars().all()

    # Card stats
    card_result = await db.execute(
        select(YouTubeCardStats)
        .where(YouTubeCardStats.creator_id == creator.id)
    )
    card_stats = card_result.scalar_one_or_none()

    return templates.TemplateResponse(request, "partials/tab_traffic.html", {
        "creator": creator,
        "traffic": traffic,
        "search_terms": search_terms,
        "card_stats": card_stats,
    })


@app.get("/creator/{slug}/tab/instagram", response_class=HTMLResponse)
async def tab_instagram(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    user, creator = await _get_creator_for_request(slug, request, db)
    return templates.TemplateResponse(request, "partials/tab_instagram.html", {
        "creator": creator,
        "current_user": user,
    })


@app.get("/creator/{slug}/video/{video_id}", response_class=HTMLResponse)
async def video_deep_dive(slug: str, video_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """On-demand video deep dive — loads within the Content tab via HTMX."""
    user, creator = await _get_creator_for_request(slug, request, db)

    result = await db.execute(
        select(YouTubeVideo).where(
            YouTubeVideo.video_id == video_id,
            YouTubeVideo.creator_id == creator.id,
        )
    )
    video = result.scalar_one_or_none()
    if not video:
        return HTMLResponse('<p class="empty-state">Video not found</p>')

    # Check for cached deep-dive data (< 6 hours old)
    analytics_result = await db.execute(
        select(YouTubeVideoAnalytics).where(YouTubeVideoAnalytics.video_id == video.id)
    )
    analytics = analytics_result.scalar_one_or_none()

    deep_dive = None
    needs_fetch = (
        analytics is None
        or analytics.retention_data is None
        or (datetime.datetime.utcnow() - analytics.last_updated).total_seconds() > 21600
    )

    await db.refresh(creator, ["user"])

    if needs_fetch:
        # Sequential — SQLAlchemy AsyncSession is NOT safe for concurrent use.
        # asyncio.gather() with the same session causes silent failures when
        # one coroutine commits while the other is mid-query.
        deep_dive = await fetch_video_deep_dive(video, creator, db)
        if not deep_dive.get("error"):
            analytics_result = await db.execute(
                select(YouTubeVideoAnalytics).where(YouTubeVideoAnalytics.video_id == video.id)
            )
            analytics = analytics_result.scalar_one_or_none()
    else:
        deep_dive = None

    pulse = await fetch_video_comments(video, creator, db)

    return templates.TemplateResponse(request, "partials/video_deep_dive.html", {
        "creator": creator,
        "video": video,
        "analytics": analytics,
        "deep_dive": deep_dive,
        "pulse": pulse,
        "error": deep_dive.get("error") if deep_dive else None,
    })


# --- Admin endpoints ---

@app.post("/admin/sync/{creator_id}")
async def trigger_sync(creator_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Admin/viewer: manually trigger YouTube sync for a creator."""
    user = await get_current_user(request, db)
    if not user or user.role not in ("admin", "viewer"):
        raise HTTPException(status_code=403)

    result = await db.execute(select(Creator).where(Creator.id == creator_id))
    creator = result.scalar_one_or_none()
    if not creator:
        raise HTTPException(status_code=404)

    await db.refresh(creator, ["user"])
    success = await sync_creator_youtube(creator, db)

    if request.headers.get("HX-Request"):
        return HTMLResponse(
            f'<span class="sync-status {"success" if success else "error"}">'
            f'{"Synced" if success else "Failed"}</span>'
        )
    return {"success": success}


@app.post("/admin/add-creator")
async def add_creator_manual(request: Request, db: AsyncSession = Depends(get_db)):
    """Admin: manually add a creator (before they log in)."""
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        raise HTTPException(status_code=403)

    form = await request.form()
    email = form.get("email", "").strip()
    name = form.get("name", "").strip()

    if not email or not name:
        raise HTTPException(status_code=400, detail="Email and name required")

    # Create user + creator
    result = await db.execute(select(User).where(User.email == email))
    existing = result.scalar_one_or_none()
    if existing:
        return RedirectResponse("/dashboard?msg=exists", status_code=302)

    new_user = User(email=email, name=name, role="creator")
    db.add(new_user)
    await db.flush()

    slug = email.split("@")[0].lower().replace(".", "-")
    creator = Creator(user_id=new_user.id, display_name=name, slug=slug)
    db.add(creator)
    await db.commit()

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/admin/delete-creator/{creator_id}")
async def delete_creator_admin(creator_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Admin: delete a creator and all associated data via the pipeline API."""
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
        raise HTTPException(status_code=403)

    async with httpx.AsyncClient() as client:
        response = await client.delete(
            f"{settings.BASE_URL}/api/creators/{creator_id}",
            headers={"x-api-key": settings.PIPELINE_API_KEY},
        )

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Creator not found")
    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if response.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail="Failed to delete creator")

    # HTMX: return an empty 200 so the row is swapped out
    return HTMLResponse("", status_code=200)
