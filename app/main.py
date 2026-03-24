"""Elusive Analytics Dashboard — FastAPI application."""
import datetime
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, init_db
from app.models import Creator, User, YouTubeStat, YouTubeVideo, YouTubeDemographic
from app.auth import handle_google_login, handle_google_callback
from app.api import router as api_router
from app.scheduler import start_scheduler, stop_scheduler
from app.youtube import sync_creator_youtube

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

    if user.role == "admin":
        return RedirectResponse("/dashboard")
    else:
        # Creator goes to their own dashboard
        result = await db.execute(
            select(Creator).where(Creator.user_id == user.id)
        )
        creator = result.scalar_one_or_none()
        if creator:
            return RedirectResponse(f"/creator/{creator.slug}")
        return RedirectResponse("/dashboard")


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

        return RedirectResponse("/", status_code=302)
    except Exception as e:
        log.error(f"OAuth callback error: {e}")
        return RedirectResponse("/login?error=auth_failed", status_code=302)

    # YouTube sync is best-effort — never blocks login
    # Use Sync Now from the admin dashboard to trigger manually


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

    # Only admin can see agency dashboard
    if user.role != "admin":
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
    })


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

    # Creators can only see their own dashboard (admin can see all)
    if user.role != "admin":
        if not creator.user_id == user.id:
            raise HTTPException(status_code=403, detail="Access denied")

    # Recent videos
    vids_result = await db.execute(
        select(YouTubeVideo)
        .where(YouTubeVideo.creator_id == creator.id)
        .order_by(YouTubeVideo.published_at.desc())
        .limit(5)
    )
    videos = vids_result.scalars().all()

    # Daily stats (30 days)
    stats_result = await db.execute(
        select(YouTubeStat)
        .where(YouTubeStat.creator_id == creator.id)
        .order_by(YouTubeStat.date.desc())
        .limit(30)
    )
    daily_stats = list(reversed(stats_result.scalars().all()))

    # Demographics
    demo_result = await db.execute(
        select(YouTubeDemographic)
        .where(YouTubeDemographic.creator_id == creator.id)
    )
    demographics = demo_result.scalars().all()
    demo_data = {}
    for d in demographics:
        demo_data.setdefault(d.dimension, []).append({"value": d.value, "percentage": d.percentage})

    return templates.TemplateResponse(request, "creator.html", {
        "user": user,
        "creator": creator,
        "videos": videos,
        "daily_stats": daily_stats,
        "demographics": demo_data,
        "page_title": creator.display_name,
    })


# --- Admin endpoints ---

@app.post("/admin/sync/{creator_id}")
async def trigger_sync(creator_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Admin: manually trigger YouTube sync for a creator."""
    user = await get_current_user(request, db)
    if not user or user.role != "admin":
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
