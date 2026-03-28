"""YouTube API expansion — new columns and tables.

Revision ID: 001_youtube_expansion
Revises: None
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = '001_youtube_expansion'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- YouTubeStat: add impressions, CTR, unique_viewers ---
    op.add_column('youtube_stats', sa.Column('impressions', sa.Integer(), nullable=True))
    op.add_column('youtube_stats', sa.Column('impressions_ctr', sa.Float(), nullable=True))
    op.add_column('youtube_stats', sa.Column('unique_viewers', sa.Integer(), nullable=True))

    # --- YouTubeVideoAnalytics: add impressions, CTR, shares ---
    op.add_column('youtube_video_analytics', sa.Column('impressions', sa.Integer(), nullable=True))
    op.add_column('youtube_video_analytics', sa.Column('impressions_ctr', sa.Float(), nullable=True))
    op.add_column('youtube_video_analytics', sa.Column('shares', sa.Integer(), nullable=True))

    # --- YouTubeDemographic: add avg_view_duration ---
    op.add_column('youtube_demographics', sa.Column('avg_view_duration', sa.Float(), nullable=True))

    # --- YouTubeVideo: add shares, tags ---
    op.add_column('youtube_videos', sa.Column('shares', sa.Integer(), nullable=True))
    op.add_column('youtube_videos', sa.Column('tags', sa.JSON(), nullable=True))

    # --- Creator: add cached impressions/CTR/uniques fields ---
    op.add_column('creators', sa.Column('yt_impressions_30d', sa.Integer(), server_default='0'))
    op.add_column('creators', sa.Column('yt_impressions_ctr', sa.Float(), server_default='0.0'))
    op.add_column('creators', sa.Column('yt_unique_viewers_30d', sa.Integer(), server_default='0'))

    # --- New table: youtube_search_terms ---
    op.create_table(
        'youtube_search_terms',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('creators.id'), nullable=False, index=True),
        sa.Column('term', sa.String(500), nullable=False),
        sa.Column('views', sa.Integer(), default=0),
        sa.Column('watch_time_minutes', sa.Float(), default=0.0),
        sa.Column('last_updated', sa.DateTime()),
    )

    # --- New table: youtube_card_stats ---
    op.create_table(
        'youtube_card_stats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('creators.id'), nullable=False, unique=True, index=True),
        sa.Column('window_start', sa.DateTime(), nullable=True),
        sa.Column('window_end', sa.DateTime(), nullable=True),
        sa.Column('card_impressions', sa.Integer(), nullable=True),
        sa.Column('card_clicks', sa.Integer(), nullable=True),
        sa.Column('card_click_rate', sa.Float(), nullable=True),
        sa.Column('card_teaser_impressions', sa.Integer(), nullable=True),
        sa.Column('card_teaser_clicks', sa.Integer(), nullable=True),
        sa.Column('card_teaser_click_rate', sa.Float(), nullable=True),
        sa.Column('last_updated', sa.DateTime()),
    )

    # --- New table: youtube_reporting_jobs ---
    op.create_table(
        'youtube_reporting_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('creators.id'), nullable=False, index=True),
        sa.Column('job_id', sa.String(100), nullable=False),
        sa.Column('report_type_id', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime()),
        sa.Column('last_downloaded_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('youtube_reporting_jobs')
    op.drop_table('youtube_card_stats')
    op.drop_table('youtube_search_terms')

    op.drop_column('creators', 'yt_unique_viewers_30d')
    op.drop_column('creators', 'yt_impressions_ctr')
    op.drop_column('creators', 'yt_impressions_30d')

    op.drop_column('youtube_videos', 'tags')
    op.drop_column('youtube_videos', 'shares')

    op.drop_column('youtube_demographics', 'avg_view_duration')

    op.drop_column('youtube_video_analytics', 'shares')
    op.drop_column('youtube_video_analytics', 'impressions_ctr')
    op.drop_column('youtube_video_analytics', 'impressions')

    op.drop_column('youtube_stats', 'unique_viewers')
    op.drop_column('youtube_stats', 'impressions_ctr')
    op.drop_column('youtube_stats', 'impressions')
