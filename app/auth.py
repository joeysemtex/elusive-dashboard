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

# Instagram OAuth registration (Instagram API with Instagram Login)
oauth.register(
    name="instagram",
    client_id=settings.META_APP_ID,
    client_secret=settings.META_APP_SECRET,
    authorize_url="https://www.instagram.com/oauth/authorize",
    access_token_url="https://api.instagram.com/oauth/access_token",
    client_kwargs={
        "scope": "instagram_business_basic,instagram_business_manage_insights",
        "token_endpoint_auth_method": "client_secret_post",
    },
)

IG_GRAPH = "https://graph.instagram.com"


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
    """Redirect to Instagram consent screen for Instagram access."""
    redirect_uri = settings.instagram_redirect_uri
    return await oauth.instagram.authorize_redirect(request, redirect_uri)


async def handle_instagram_callback(request: Request, db: AsyncSession) -> dict:
    """Process Instagram OAuth callback. Connects IG to existing user.

    Instagram API with Instagram Login flow:
      1. Exchange auth code → short-lived token (api.instagram.com)
      2. Exchange short-lived → long-lived token (graph.instagram.com)
      3. Fetch profile via /me endpoint

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

    # Step 1: Exchange auth code for short-lived token (api.instagram.com)
    code = request.query_params.get("code", "")
    if not code:
        log.error("Instagram OAuth: no code in callback")
        return {"success": False, "error": "no_code"}

    async with httpx.AsyncClient() as client:
        # Exchange code → short-lived token (form-encoded POST)
        resp = await client.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": settings.instagram_redirect_uri,
            "code": code,
        })
        if resp.status_code != 200:
            log.error(f"Instagram token exchange failed ({resp.status_code}): {resp.text}")
            return {"success": False, "error": "no_token"}

        token_data = resp.json()
        short_token = token_data.get("access_token", "")
        ig_user_id = str(token_data.get("user_id", ""))
        if not short_token:
            log.error(f"Instagram OAuth: no access_token in response: {token_data}")
            return {"success": False, "error": "no_token"}

        # Step 2: Exchange short-lived → long-lived token (graph.instagram.com)
        resp = await client.get(f"{IG_GRAPH}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.META_APP_SECRET,
            "access_token": short_token,
        })
        if resp.status_code != 200:
            log.error(f"Instagram long-lived token exchange failed: {resp.text}")
            return {"success": False, "error": "token_exchange_failed"}

        ll_data = resp.json()
        long_token = ll_data.get("access_token", "")
        expires_in = ll_data.get("expires_in", 5184000)  # default 60 days

        # Step 3: Fetch profile via /me
        resp = await client.get(f"{IG_GRAPH}/me", params={
            "access_token": long_token,
            "fields": "user_id,username,name,account_type,profile_picture_url,followers_count,media_count",
        })
        if resp.status_code != 200:
            log.error(f"Instagram profile fetch failed: {resp.text}")
            return {"success": False, "error": "profile_fetch_failed"}

        profile = resp.json()
        ig_account_id = str(profile.get("user_id", ig_user_id))

    # Step 4: Store on User model
    user.instagram_access_token = encrypt_token(long_token)
    user.instagram_token_expiry = (
        datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    )
    user.instagram_user_id = ig_account_id

    # Step 5: Store on Creator model
    creator.instagram_account_id = ig_account_id
    creator.instagram_username = profile.get("username", "")
    creator.ig_followers = profile.get("followers_count", 0)

    await db.commit()
    log.info(f"Instagram connected for {user.email}: @{creator.instagram_username}")
    return {"success": True, "slug": creator.slug}
