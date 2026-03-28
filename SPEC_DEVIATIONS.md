# SPEC_DEVIATIONS.md — YouTube API Expansion

Deviations, improvements, and additions beyond `YOUTUBE_API_EXPANSION_SPEC.md`.

---

## Elevations (things added beyond spec)

### 1. Video tags captured from Data API
- **What:** Added `tags` (JSON) column to `YouTubeVideo` and `tags` field to all API responses.
- **Why:** Tags are free metadata from the `snippet` part already being fetched. They're useful for content categorisation in pitch decks and for understanding SEO strategy.

### 2. Video-level shares on the `YouTubeVideo` model
- **What:** Added `shares` column to `YouTubeVideo` in addition to `YouTubeVideoAnalytics.shares`.
- **Why:** The batch analytics query returns shares per video. Storing it directly on the video record (not just the analytics record) means the shares field is available even when video analytics haven't been fetched via deep dive. Makes the data more accessible.

### 3. Per-video search terms in deep dive
- **What:** Added a 6th query to `fetch_video_deep_dive` that fetches the top 10 YouTube search terms for each video using `insightTrafficSourceDetail` with a video filter.
- **Why:** Knowing which search terms drove views to a specific video is high-value for pitch decks — it shows the creator's SEO authority on specific topics.

### 4. Country demographics expanded to top 25 (was top 10)
- **What:** Changed `maxResults` for country query from 10 to 25.
- **Why:** For gaming creators with global audiences, top 10 countries misses significant audience segments. 25 covers 95%+ of views for most channels with negligible extra cost.

### 5. Reporting API report type version matching (spec A7)
- **What:** Implemented fuzzy prefix matching for report type IDs and automatic selection of the newest version.
- **Why:** Spec hardcoded `_a2` suffixes but Google periodically releases newer versions (`_a3`). The implementation calls `reportTypes.list()`, matches against prefixes, and picks the highest version. This makes the integration forward-compatible.

### 6. Cached impressions/CTR/uniques on Creator model
- **What:** Added `yt_impressions_30d`, `yt_impressions_ctr`, and `yt_unique_viewers_30d` columns on the `Creator` model.
- **Why:** The `/api/creators` list endpoint needs these aggregates without joining to daily stats for every creator. Pre-computing them in `_calculate_metrics` keeps the list endpoint fast — same pattern as existing `yt_30d_views` and `yt_engagement_rate`.

### 7. Demographics delete scoped to non-subscribedStatus
- **What:** Changed the demographics clear to `dimension != "subscribedStatus"` instead of blanket delete.
- **Why:** `_sync_demographics` and `_sync_subscribed_status` are separate functions. The old code deleted ALL demographics at the start of `_sync_demographics`, then `_sync_subscribed_status` ran separately and added its own rows. If demographics sync succeeded but subscribed status failed, the old subscribedStatus data was already gone. Scoping the delete prevents this race.

---

## Architectural decisions

### 1. `uniques` metric handling
- **What:** The `uniques` metric is NOT included in the daily stats query. The Analytics API only supports `uniques` without a `day` dimension or with `month` dimension — not per-day.
- **Why:** Including `uniques` in the day-dimension query would cause a 400 error. The `unique_viewers` column on `YouTubeStat` is left nullable and will be populated by the Reporting API (`channel_combined_a2`) when those reports become available (24-48h after job creation). For now, the daily unique count stays null.

### 2. Reporting API ingestion functions for traffic/playback/device are stubs
- **What:** `_ingest_traffic_source_report`, `_ingest_playback_location_report`, and `_ingest_device_os_report` currently just count and log rows.
- **Why:** The Analytics API already provides this data fresh. The Reporting API versions add historical depth but the ingestion logic needs careful deduplication against existing Analytics API data. Stubs ensure the jobs are registered and reports are downloaded without risking data corruption. Full ingestion can be added incrementally.

### 3. Window standardisation
- **What:** All analytics queries now use a consistent 90-day window (was mixed 30/60/90).
- **Why:** Spec note on consistency. `_calculate_metrics` still uses the most recent 30 daily stat rows for the Creator aggregate fields (30d views, engagement, etc.) — the window for *storage* is 90 days, the window for *aggregation* remains 30 days.

### 4. `channel_combined_a2` ingestion — impressions-only update
- **What:** When ingesting Reporting API data, only `impressions` and `impressions_ctr` are updated on existing `YouTubeStat` rows. Other fields (views, watch time) are NOT overwritten.
- **Why:** The Analytics API provides more granular, real-time data. The Reporting API's value is backfilling impressions for dates older than the 90-day Analytics API window. Overwriting would risk replacing fresher data with stale daily reports.
