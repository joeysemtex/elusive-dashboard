"""Google OAuth 2.0 authentication."""
import datetime
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from starlette.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import encrypt_token
from app.models import User, Creator

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
        "prompt": "consent",
        "access_type": "offline",
    },
)


async def handle_google_login(request: Request):
    """Redirect to Google OAuth consent screen."""
    redirect_uri = settings.google_redirect_uri
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
        role = "admin" if email == settings.ADMIN_EMAIL else "creator"

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

        # Auto-create a creator profile for non-admin users
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
