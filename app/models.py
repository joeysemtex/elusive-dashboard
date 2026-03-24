import datetime
from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text, JSON
)
from sqlalchemy.orm import relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    avatar_url = Column(String(512), nullable=True)
    role = Column(String(20), nullable=False, default="creator")  # "admin" or "creator"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    # Google OAuth tokens (for YouTube access)
    google_access_token = Column(Text, nullable=True)   # encrypted
    google_refresh_token = Column(Text, nullable=True)   # encrypted
    google_token_expiry = Column(DateTime, nullable=True)

    # Instagram tokens — Phase 2
    instagram_access_token = Column(Text, nullable=True)
    instagram_token_expiry = Column(DateTime, nullable=True)
    instagram_user_id = Column(String(100), nullable=True)

    creator = relationship("Creator", back_populates="user", uselist=False)


class Creator(Base):
    __tablename__ = "creators"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True)
    display_name = Column(String(255), nullable=False)
    slug = Column(String(100), unique=True, nullable=False, index=True)
    avatar_url = Column(String(512), nullable=True)

    # YouTube channel info
    youtube_channel_id = Column(String(100), nullable=True, index=True)
    youtube_channel_title = Column(String(255), nullable=True)
    youtube_channel_url = Column(String(512), nullable=True)

    # Instagram — Phase 2
    instagram_username = Column(String(100), nullable=True)
    instagram_account_id = Column(String(100), nullable=True)

    # Cached aggregate stats (updated by scheduler)
    yt_subscribers = Column(Integer, default=0)
    yt_total_views = Column(Integer, default=0)
    yt_video_count = Column(Integer, default=0)
    yt_30d_views = Column(Integer, default=0)
    yt_engagement_rate = Column(Float, default=0.0)
    yt_avg_view_duration = Column(Float, default=0.0)  # seconds

    ig_followers = Column(Integer, default=0)
    ig_reach_30d = Column(Integer, default=0)
    ig_engagement_rate = Column(Float, default=0.0)

    # Trend direction: "growing", "stable", "declining"
    trend_direction = Column(String(20), default="stable")

    last_yt_sync = Column(DateTime, nullable=True)
    last_ig_sync = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="creator")
    youtube_stats = relationship("YouTubeStat", back_populates="creator", order_by="desc(YouTubeStat.date)")
    youtube_videos = relationship("YouTubeVideo", back_populates="creator", order_by="desc(YouTubeVideo.published_at)")


class YouTubeStat(Base):
    """Daily YouTube stats snapshot for trend tracking."""
    __tablename__ = "youtube_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    creator_id = Column(Integer, ForeignKey("creators.id"), nullable=False, index=True)
    date = Column(DateTime, nullable=False, index=True)
    views = Column(Integer, default=0)
    subscribers_gained = Column(Integer, default=0)
    subscribers_lost = Column(Integer, default=0)
    watch_time_minutes = Column(Float, default=0.0)
    avg_view_duration = Column(Float, default=0.0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    creator = relationship("Creator", back_populates="youtube_stats")


class YouTubeVideo(Base):
    """Individual video stats."""
    __tablename__ = "youtube_videos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    creator_id = Column(Integer, ForeignKey("creators.id"), nullable=False, index=True)
    video_id = Column(String(50), unique=True, nullable=False, index=True)
    title = Column(String(500), nullable=False)
    thumbnail_url = Column(String(512), nullable=True)
    published_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, default=0)
    views = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    engagement_rate = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow)

    creator = relationship("Creator", back_populates="youtube_videos")


class YouTubeDemographic(Base):
    """Audience demographics from YouTube Analytics."""
    __tablename__ = "youtube_demographics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    creator_id = Column(Integer, ForeignKey("creators.id"), nullable=False, index=True)
    dimension = Column(String(50), nullable=False)  # "ageGroup", "gender", "country"
    value = Column(String(100), nullable=False)       # e.g. "age25-34", "male", "US"
    percentage = Column(Float, default=0.0)
    last_updated = Column(DateTime, default=datetime.datetime.utcnow)
