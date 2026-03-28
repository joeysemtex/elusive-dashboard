"""Instagram Graph API client — token management, sync, and deletion helpers."""
import datetime
import logging
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_token, encrypt_token
from app.config import settings
from app.models import Creator, User

log = logging.getLogger("elusive.instagram")

IG_GRAPH = "https://graph.instagram.com"
IG_REFRESH_URL = "https://graph.instagram.com/refresh_access_token"


# --- Token Management ---


async def _refresh_instagram_token(user: User, db: AsyncSession) -> Optional[str]:
    """Refresh a long-lived Instagram token (valid for another 60 days)."""
    current_token = decrypt_token(user.instagram_access_token or "")
    if not current_token:
        log.warning(f"No Instagram token to refresh for {user.email}")
        return None

    async with httpx.AsyncClient() as client:
        resp = await client.get(IG_REFRESH_URL, params={
            "grant_type": "ig_refresh_token",
            "access_token": current_token,
        })

    if resp.status_code != 200:
        log.error(f"Instagram token refresh failed for {user.email}: {resp.text}")
        return None

    data = resp.json()
    new_token = data.get("access_token", "")
    expires_in = data.get("expires_in", 5184000)

    user.instagram_access_token = encrypt_token(new_token)
    user.instagram_token_expiry = (
        datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    )
    await db.commit()
    log.info(f"Instagram token refreshed for {user.email}")
    return new_token


async def _get_valid_ig_token(user: User, db: AsyncSession) -> Optional[str]:
    """Get a valid Instagram access token, refreshing proactively if near expiry."""
    if not user.instagram_access_token:
        return None

    token = decrypt_token(user.instagram_access_token)
    if not token:
        return None

    # Proactively refresh if within 7 days of expiry
    if user.instagram_token_expiry:
        days_remaining = (user.instagram_token_expiry - datetime.datetime.utcnow()).days
        if days_remaining < 0:
            log.warning(f"Instagram token expired for {user.email}")
            return None
        if days_remaining < 7:
            refreshed = await _refresh_instagram_token(user, db)
            return refreshed or token

    return token


# --- API Helper ---


async def _ig_get(token: str, endpoint: str, params: Optional[dict] = None) -> Optional[dict]:
    """Authenticated GET to Instagram Graph API."""
    url = f"{IG_GRAPH}/{endpoint}"
    request_params = {"access_token": token}
    if params:
        request_params.update(params)

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=request_params)

    if resp.status_code != 200:
        log.error(f"Instagram API error ({resp.status_code}) for {endpoint}: {resp.text}")
        return None

    return resp.json()


# --- Sync ---


async def sync_creator_instagram(creator: Creator, db: AsyncSession) -> bool:
    """Sync Instagram metrics for a creator. Returns True on success."""
    await db.refresh(creator, ["user"])
    user = creator.user

    if not creator.instagram_account_id:
        return False

    token = await _get_valid_ig_token(user, db)
    if not token:
        log.warning(f"No valid Instagram token for {creator.display_name}")
        return False

    ig_id = creator.instagram_account_id

    # Call 1 — Profile stats
    profile = await _ig_get(token, ig_id, {
        "fields": "followers_count,media_count,username,profile_picture_url",
    })
    if profile:
        creator.ig_followers = profile.get("followers_count", creator.ig_followers)
        creator.instagram_username = profile.get("username", creator.instagram_username)

    # Call 2 — 30-day reach (account insights)
    now = datetime.datetime.utcnow()
    since = int((now - datetime.timedelta(days=30)).timestamp())
    until = int(now.timestamp())

    insights = await _ig_get(token, f"{ig_id}/insights", {
        "metric": "reach",
        "period": "day",
        "since": str(since),
        "until": str(until),
    })
    if insights and "data" in insights:
        total_reach = 0
        for metric in insights["data"]:
            if metric.get("name") == "reach":
                for val in metric.get("values", []):
                    total_reach += val.get("value", 0)
        creator.ig_reach_30d = total_reach

    # Call 3 — Recent media (engagement rate)
    media = await _ig_get(token, f"{ig_id}/media", {
        "fields": "id,media_type,timestamp,like_count,comments_count",
        "limit": "25",
    })
    if media and "data" in media:
        posts = media["data"]
        if posts and creator.ig_followers and creator.ig_followers > 0:
            total_engagement = sum(
                (p.get("like_count", 0) + p.get("comments_count", 0))
                for p in posts
            )
            creator.ig_engagement_rate = round(
                total_engagement / (len(posts) * creator.ig_followers) * 100, 2
            )

    creator.last_ig_sync = datetime.datetime.utcnow()
    await db.commit()
    log.info(f"Instagram sync complete for {creator.display_name}: "
             f"{creator.ig_followers} followers, {creator.ig_reach_30d} reach")
    return True


# --- Token Refresh (for scheduler) ---


async def refresh_all_instagram_tokens(db: AsyncSession):
    """Refresh Instagram tokens that are within 7 days of expiry."""
    cutoff = datetime.datetime.utcnow() + datetime.timedelta(days=7)
    result = await db.execute(
        select(User).where(
            User.instagram_token_expiry != None,
            User.instagram_token_expiry <= cutoff,
            User.instagram_access_token != None,
        )
    )
    users = result.scalars().all()

    refreshed = 0
    for user in users:
        token = await _refresh_instagram_token(user, db)
        if token:
            refreshed += 1

    if users:
        log.info(f"Instagram token refresh: {refreshed}/{len(users)} refreshed")


# --- Data Deletion Helpers (Meta compliance) ---


async def clear_instagram_data(user: User, creator: Optional[Creator], db: AsyncSession):
    """Remove all Instagram data for a user (data deletion request)."""
    user.instagram_access_token = None
    user.instagram_token_expiry = None
    user.instagram_user_id = None

    if creator:
        creator.instagram_account_id = None
        creator.instagram_username = None
        creator.ig_followers = 0
        creator.ig_reach_30d = 0
        creator.ig_engagement_rate = 0.0
        creator.last_ig_sync = None

    await db.commit()
    log.info(f"Instagram data cleared for {user.email}")


async def clear_instagram_tokens(user: User, db: AsyncSession):
    """Clear Instagram tokens only (deauthorization — keep stats for display)."""
    user.instagram_access_token = None
    user.instagram_token_expiry = None
    await db.commit()
    log.info(f"Instagram tokens cleared for {user.email} (deauthorized)")
