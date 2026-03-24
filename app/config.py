import os
import secrets
from dotenv import load_dotenv

load_dotenv()


class Settings:
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    META_APP_ID: str = os.getenv("META_APP_ID", "")
    META_APP_SECRET: str = os.getenv("META_APP_SECRET", "")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./dev.db")
    SECRET_KEY: str = os.getenv("SECRET_KEY", secrets.token_hex(32))
    FERNET_KEY: str = os.getenv("FERNET_KEY", "")
    BASE_URL: str = os.getenv("BASE_URL", "http://localhost:8000")
    PIPELINE_API_KEY: str = os.getenv("PIPELINE_API_KEY", secrets.token_hex(16))
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "josephjsemaan@gmail.com")
    YOUTUBE_REFRESH_HOURS: int = 6

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.BASE_URL}/auth/google/callback"

    @property
    def instagram_redirect_uri(self) -> str:
        return f"{self.BASE_URL}/auth/instagram/callback"


settings = Settings()
