"""Google + Instagram OAuth authentication."""
import datetime
import logging

import httpx
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import encrypt_token
from app.models import User, Creator

log = logging.getLogger("elusive.auth")

oauth = OAuth()

# Google OAuth registration
oauth.register(
    name="google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": (
            "openid email profile "
            "https://www.googleapis.com/auth/youtube.readonly "
            "https://www.googleapis.com/auth/yt-analytics.readonly"
        ),
    },
    authorize_params={
        "access_type": "offline",
        "prompt": "consent",
    },
)

# Instagram / Meta OAuth registration
oauth.register(
    name="instagram",
    client_id=settings.META_APP_ID,
    client_secret=settings.META_APP_SECRET,
    authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
    access_token_url="https://graph.facebook.com/v21.0/oauth/access_token",
    client_kwargs={
        "scope": "instagram_basic,instagram_manage_insights,pages_show_list,pages_read_engagement",
    },
)

GRAPH_BASE = "https://graph.facebook.com/v21.0"


async def handle_google_login(request: Request):
    """Redirect to Google OAuth consent screen.

    Derives the callback URL from the request itself rather than
    building it from BASE_URL, avoiding doubled-path bugs.
    """
    redirect_uri = str(request.url_for("google_callback"))
    # Railway proxy may report http; force https in production
    if redirect_uri.startswith("http://") and settings.BASE_URL.startswith("https://"):
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return await oauth.google.authorize_redirect(request, redirect_uri)


async def handle_google_callback(request: Request, db: AsyncSession) -> User:
    """Process Google OAuth callback. Returns the user."""
    token_data = await oauth.google.authorize_access_token(request)

    # Extract user info from ID token
    userinfo = token_data.get("userinfo", {})
    email = userinfo.get("email", "")
    name = userinfo.get("name", email.split("@")[0])
    picture = userinfo.get("picture", "")

    # Check if user exists
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_at = token_data.get("expires_at")
    token_expiry = (
        datetime.datetime.utcfromtimestamp(expires_at) if expires_at else None
    )

    if user is None:
        # Determine role
        if email == settings.ADMIN_EMAIL:
            role = "admin"
        elif email in settings.VIEWER_EMAILS:
            role = "viewer"
        else:
            role = "creator"

        user = User(
            email=email,
            name=name,
            avatar_url=picture,
            role=role,
            google_access_token=encrypt_token(access_token),
            google_refresh_token=encrypt_token(refresh_token) if refresh_token else None,
            google_token_expiry=token_expiry,
            last_login=datetime.datetime.utcnow(),
        )
        db.add(user)
        await db.flush()

        # Auto-create a creator profile for creator users only
        if role == "creator":
            slug = email.split("@")[0].lower().replace(".", "-")
            creator = Creator(
                user_id=user.id,
                display_name=name,
                slug=slug,
                avatar_url=picture,
            )
            db.add(creator)
    else:
        # Update tokens and login time
        user.google_access_token = encrypt_token(access_token)
        if refresh_token:
            user.google_refresh_token = encrypt_token(refresh_token)
        user.google_token_expiry = token_expiry
        user.last_login = datetime.datetime.utcnow()
        user.name = name
        user.avatar_url = picture

    await db.commit()
    await db.refresh(user)
    return user


# --- Instagram OAuth (connect flow — user already logged in via Google) ---


async def handle_instagram_login(request: Request):
    """Redirect to Facebook/Meta consent screen for Instagram access."""
    redirect_uri = str(request.url_for("instagram_callback"))
    if redirect_uri.startswith("http://") and settings.BASE_URL.startswith("https://"):
        redirect_uri = redirect_uri.replace("http://", "https://", 1)
    return await oauth.instagram.authorize_redirect(request, redirect_uri)


async def handle_instagram_callback(request: Request, db: AsyncSession) -> dict:
    """Process Instagram OAuth callback. Connects IG to existing user.

    Returns dict with 'success' bool and optional 'error' key.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return {"success": False, "error": "not_logged_in"}

    # Load user + creator
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return {"success": False, "error": "user_not_found"}

    creator_result = await db.execute(select(Creator).where(Creator.user_id == user.id))
    creator = creator_result.scalar_one_or_none()
    if not creator:
        return {"success": False, "error": "no_creator_profile"}

    # Step 1: Exchange auth code for short-lived token
    token_data = await oauth.instagram.authorize_access_token(request)
    short_token = token_data.get("access_token", "")
    if not short_token:
        log.error("Instagram OAuth: no access_token in response")
        return {"success": False, "error": "no_token"}

    async with httpx.AsyncClient() as client:
        # Step 2: Exchange short-lived → long-lived token (60 days)
        resp = await client.get(f"{GRAPH_BASE}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "fb_exchange_token": short_token,
        })
        if resp.status_code != 200:
            log.error(f"Instagram long-lived token exchange failed: {resp.text}")
            return {"success": False, "error": "token_exchange_failed"}

        ll_data = resp.json()
        long_token = ll_data.get("access_token", "")
        expires_in = ll_data.get("expires_in", 5184000)  # default 60 days

        # Step 3: Discover Instagram Business/Creator Account
        resp = await client.get(f"{GRAPH_BASE}/me/accounts", params={
            "access_token": long_token,
            "fields": "id,name,instagram_business_account",
        })
        if resp.status_code != 200:
            log.error(f"Instagram page discovery failed: {resp.text}")
            return {"success": False, "error": "page_discovery_failed"}

        pages = resp.json().get("data", [])
        ig_account_id = None
        for page in pages:
            ig_biz = page.get("instagram_business_account")
            if ig_biz:
                ig_account_id = ig_biz.get("id")
                break

        if not ig_account_id:
            log.warning(f"No Instagram Business account found for user {user.email}")
            return {"success": False, "error": "no_ig_business"}

        # Step 4: Fetch IG profile info
        resp = await client.get(f"{GRAPH_BASE}/{ig_account_id}", params={
            "access_token": long_token,
            "fields": "username,followers_count,profile_picture_url",
        })
        if resp.status_code != 200:
            log.error(f"Instagram profile fetch failed: {resp.text}")
            return {"success": False, "error": "profile_fetch_failed"}

        profile = resp.json()

    # Step 5: Store on User model
    user.instagram_access_token = encrypt_token(long_token)
    user.instagram_token_expiry = (
        datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    )
    user.instagram_user_id = ig_account_id

    # Step 6: Store on Creator model
    creator.instagram_account_id = ig_account_id
    creator.instagram_username = profile.get("username", "")
    creator.ig_followers = profile.get("followers_count", 0)

    await db.commit()
    log.info(f"Instagram connected for {user.email}: @{creator.instagram_username}")
    return {"success": True, "slug": creator.slug}
