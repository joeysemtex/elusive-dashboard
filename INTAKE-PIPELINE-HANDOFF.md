# Claude Code Handoff — Dashboard API Integration
## Replacing Manual CSV Intake with Live Dashboard API

**Status:** Ready to implement
**Priority:** High — eliminates the single biggest friction point in the intake pipeline
**Written by:** Claude (Cowork session, 28 March 2026)

---

## What This Changes

The current intake pipeline (Steps 1-2 of the data-intake skill) requires Joey to manually export CSVs from YouTube Studio, drop them into a Talent folder, then trigger the intake. This handoff replaces that entire flow with a direct API call to this dashboard.

**Before:** Joey exports CSVs → drops into Talent folder → runs intake
**After:** Joey says "run data intake on [Creator]" → intake asks a few scoping questions → hits the API → processes everything automatically

---

## The API

**Base URL:** `https://dashboard.elusivemgmt.com`
**Auth:** `X-Api-Key` header
**Key:** `PIPELINE_API_KEY` in `.env` — read at runtime via `python-dotenv`. Never hardcode.

### Relevant Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /api/creators` | All creators with summary stats + IDs |
| `GET /api/creators/{id}/youtube` | Full data: channel stats, per-video list, daily stats (30d), demographics, long-form vs shorts split |
| `GET /api/creators/{id}/export` | Pitch-ready summary: top 5 videos, key metrics |

### Full Data Available via `/api/creators/{id}/youtube`

Verified live 28 March 2026:

**Channel level:** subscribers, total views, video count, views (30d), avg daily views, engagement rate, avg view duration (seconds), trend, last sync timestamp

**Per-video:** title, video ID, thumbnail URL, published date, duration (seconds), views, likes, comments, engagement rate, format (`long_form` / `short`)

**Daily stats (30 days):** date, views, watch_time_minutes, likes, comments

**Demographics:** age groups (%), gender (%), top countries (%), device type (%)

**Content split:** `long_form` object + `shorts` object — each with count and video list

### What the API Does NOT Currently Return (gaps vs. old CSV method)

| Missing field | Impact on intake | Resolution |
|---|---|---|
| Impressions (total) | Can't calculate impressions-based metrics | Flag as unavailable in intake output |
| Impressions CTR | Rarely surfaced anyway (rule: only if >4%) | Flag as unavailable |
| Avg % viewed per video | Strong pitch metric | Flag; use avg_view_duration/duration_seconds as proxy |
| Subscribers gained/lost (daily) | Net sub change only from summary | Use `yt_net_subscribers_30d` |
| Returning vs new viewer % | Loyalty signal for endemic brands | Flag as unavailable |
| Traffic sources breakdown | Not in API | Flag as unavailable |

> These fields are already in the database (`youtube_stats`, `youtube_video_analytics`, `youtube_traffic_sources`, `youtube_demographics` tables). They just need to be exposed in `app/routes/api.py`. See the "Future Dashboard Updates" section at the bottom.

---

## Creator ID Map (live as of 28 March 2026)

| Creator name (dashboard) | ID | Notes |
|---|---|---|
| Rain Nebula | 19 | Nebula CS2 channel |
| Vivienne Quach | 17 | BibiahnCS2 |
| Bonathan | 20 | BonathanCS |
| voocsgo | 21 | vooCSGO main |
| 2voo | 23 | its voo / secondary Voo channel |
| Riftlab | 24 | RiftlabTCG |
| Koady Boyd | 25 | Maps to Rival/RivalRVN roster entry |
| Nebula | 27 | nebulaisacoolguy |
| The Casual Athlete | 18 | NRL Fantasy |
| fog | 29 | New addition |

> Name matching: when Joey says a creator name, match against the dashboard `name` field with a fuzzy/lowercase match. If ambiguous, show Joey the options and ask. Never assume silently.

---

## New Intake Flow (what to build in `scripts/dashboard_intake.py`)

### Step 0 — AskUserQuestion (before touching the API)

When Joey says "run data intake on [Creator]", ask these questions via `AskUserQuestion` before starting:

```
Q1 (header: "Window"):
  "What time window should I analyse?"
  Options:
    - Last 30 days  ← (recommended — matches dashboard sync)
    - Last 60 days  ← (uses daily_stats, more context on slower channels)
    - All available data  ← (full video history — for low-upload creators)

Q2 (header: "Purpose") — skip if already clear from context:
  "What's this intake for?"
  Options:
    - Full pitch intake (PDF + auto-populate configs)  ← recommended
    - Quick health check (chat summary only, no PDF)
    - Specific brand pitch  ← [ask for brand name as follow-up]

Q3 (header: "Platforms") — only ask if creator has non-zero Instagram data:
  "Include Instagram?"
  Options:
    - YouTube only
    - YouTube + Instagram (from dashboard)
    - YouTube + Instagram + I'll provide extra IG data manually
```

Do not ask more than 3 questions. Everything else resolves from the API or context.

### Step 1 — Fetch and Resolve Creator

```python
import httpx, os
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("PIPELINE_API_KEY")
BASE_URL = "https://dashboard.elusivemgmt.com"
HEADERS = {"X-Api-Key": API_KEY}

def get_all_creators():
    r = httpx.get(f"{BASE_URL}/api/creators", headers=HEADERS)
    r.raise_for_status()
    return r.json()["creators"]

def resolve_creator(name_query: str, creators: list) -> dict:
    """Fuzzy match creator name. Returns match or raises if ambiguous/not found."""
    name_lower = name_query.lower()
    matches = [c for c in creators if name_lower in c["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        raise ValueError(f"Ambiguous: {[c['name'] for c in matches]}")
    raise ValueError(f"No creator found matching '{name_query}'")
```

### Step 2 — Check Freshness

```python
from datetime import datetime, timezone

def check_data_freshness(last_sync_str: str) -> tuple[bool, str]:
    """Returns (is_fresh, age_description). Warn if >24h, hard fail if >7d."""
    if not last_sync_str:
        return False, "never synced"
    last_sync = datetime.fromisoformat(last_sync_str)
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - last_sync).total_seconds() / 3600
    if age_hours < 24:
        return True, f"{int(age_hours)}h old"
    elif age_hours < 168:
        return False, f"{int(age_hours)}h old — consider syncing at /admin/sync/{creator_id}"
    else:
        return False, f"{int(age_hours // 24)} days old — must sync first"
```

If stale: *"[Creator]'s data was last synced [X] hours ago — want me to proceed anyway or wait for a fresh sync at dashboard.elusivemgmt.com/admin/sync/{id}?"*

### Step 3 — Fetch Full Data

```python
def fetch_youtube_data(creator_id: int) -> dict:
    r = httpx.get(f"{BASE_URL}/api/creators/{creator_id}/youtube", headers=HEADERS)
    r.raise_for_status()
    return r.json()
```

### Step 4 — Map API Response to Intake Metrics Dict

This produces the same metrics dict shape the rest of the pipeline expects — so Steps 3-8 of the skill run unchanged.

```python
import statistics

def map_api_to_intake_metrics(yt_data: dict, window_days: int = 30) -> dict:
    creator = yt_data["creator"]
    videos = yt_data.get("videos", [])
    daily_stats = yt_data.get("daily_stats", [])
    demographics = yt_data.get("demographics", {})
    long_form_videos = yt_data.get("long_form", {}).get("videos", [])
    shorts_videos = yt_data.get("shorts", {}).get("videos", [])

    # Conservative average — long-form only, outliers excluded (>2.5x mean)
    view_counts = [v["views"] for v in long_form_videos if v["views"] > 0]
    if view_counts:
        mean_views = statistics.mean(view_counts)
        clean_views = [v for v in view_counts if v <= 2.5 * mean_views]
        outlier_videos = [v for v in long_form_videos if v["views"] > 2.5 * mean_views]
        conservative_avg = int(statistics.mean(clean_views)) if clean_views else int(mean_views)
    else:
        conservative_avg = 0
        outlier_videos = []

    # Watch time from daily stats
    total_watch_hours = sum(d.get("watch_time_minutes", 0) for d in daily_stats) / 60

    # Demographics
    age_groups = demographics.get("ageGroup", [])
    top_age = max(age_groups, key=lambda x: x["percentage"], default={}).get("value", "unknown")
    gender = demographics.get("gender", [])
    male_pct = next((g["percentage"] for g in gender if g["value"] == "male"), None)
    countries = demographics.get("country", [])
    top_countries = [c["value"] for c in sorted(countries, key=lambda x: -x["percentage"])[:3]]
    devices = demographics.get("deviceType", [])

    avg_dur = creator.get("avg_view_duration_seconds", 0) or 0
    m, s = divmod(int(avg_dur), 60)
    avg_dur_fmt = f"{m}m {s:02d}s" if avg_dur else "N/A"

    return {
        # Source metadata
        "source": "elusive-dashboard-api",
        "api_last_sync": creator.get("last_yt_sync"),
        "analysis_window_days": window_days,

        # Channel
        "channel_title": creator.get("channel_title"),
        "channel_id": creator.get("channel_id"),
        "subscribers": creator.get("subscribers"),
        "total_views_alltime": creator.get("total_views"),
        "video_count": creator.get("video_count"),
        "trend": creator.get("trend"),

        # Performance
        "views_in_window": creator.get("views_30d"),
        "avg_daily_views": (creator.get("views_30d", 0) or 0) // 30,
        "watch_time_hours": round(total_watch_hours, 1),
        "avg_view_duration_seconds": avg_dur,
        "avg_view_duration_formatted": avg_dur_fmt,
        "engagement_rate": creator.get("engagement_rate"),

        # Conservative average
        "conservative_avg_views": conservative_avg,
        "outlier_videos": outlier_videos,
        "long_form_count": yt_data.get("long_form", {}).get("count", 0),
        "shorts_count": yt_data.get("shorts", {}).get("count", 0),

        # Video table (top 8 long-form by views)
        "top_videos": sorted(long_form_videos, key=lambda x: -x["views"])[:8],
        "all_videos": videos,

        # Demographics
        "demo_top_age": top_age,
        "demo_male_pct": male_pct,
        "demo_top_countries": top_countries,
        "demo_devices": devices,
        "demo_age_full": age_groups,
        "demo_gender_full": gender,
        "demo_country_full": countries,

        # Daily stats (for trend chart)
        "daily_stats": daily_stats,

        # Gaps — not available from API (flag these in intake output, do not ask for CSVs)
        "impressions": None,
        "impressions_ctr": None,
        "avg_pct_viewed": None,
        "returning_viewer_pct": None,
        "traffic_sources": None,
        "subscribers_gained": None,
    }
```

### Step 5 — Updated Data Source Priority (replaces SKILL.md Step 1A/1B)

```
DATA SOURCE PRIORITY ORDER

1. Dashboard API (PRIMARY — try first for all creators)
   → Hit /api/creators, match name, check freshness
   → Fresh (<24h): use automatically, no questions
   → Stale (24h-7d): warn Joey, offer to proceed or sync first
   → Very stale (>7d): require sync before proceeding

2. Talent folder CSVs (FALLBACK — for creators NOT yet on dashboard)
   → Check for Youtube CSV Stats [date] folder
   → Apply existing freshness rules (≤ 14 days)
   → Same CSV parsing pipeline as before

3. Manual entry (LAST RESORT)
   → Ask Joey for specific missing fields
   → Flag as "manually reported" in output
```

---

## Entry Point — `scripts/intake_runner.py`

Create this as the single entry point for all intake runs:

```python
"""
intake_runner.py — Entry point for elusive-data-intake pipeline.

Usage:
    python scripts/intake_runner.py --creator "Rain Nebula" --window 30 --purpose pitch

Arguments:
    --creator   Creator name (fuzzy matched against dashboard)
    --window    Analysis window in days (30, 60, or 90). Default: 30
    --purpose   "pitch" | "health_check" | "brand_pitch:<brand_name>"
    --brand     Brand name (used when purpose=brand_pitch)
"""

import argparse
from dashboard_intake import get_all_creators, resolve_creator, fetch_youtube_data, \
                             map_api_to_intake_metrics, check_data_freshness

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--creator", required=True)
    parser.add_argument("--window", type=int, default=30)
    parser.add_argument("--purpose", default="pitch")
    parser.add_argument("--brand", default=None)
    args = parser.parse_args()

    # 1. Resolve creator
    creators = get_all_creators()
    creator_summary = resolve_creator(args.creator, creators)
    creator_id = creator_summary["id"]

    # 2. Fetch full data
    yt_data = fetch_youtube_data(creator_id)

    # 3. Check freshness
    is_fresh, age_desc = check_data_freshness(yt_data["creator"].get("last_yt_sync", ""))
    if not is_fresh:
        print(f"WARNING: Data is {age_desc}. Consider triggering a sync first.")

    # 4. Map to metrics
    metrics = map_api_to_intake_metrics(yt_data, window_days=args.window)

    # 5. Hand off to existing pipeline (Steps 3-8 of SKILL.md)
    # ... run analysis, generate PDF, populate configs ...
    print(f"Intake complete for {creator_summary['name']}")
    print(f"Conservative avg views: {metrics['conservative_avg_views']:,}")
    print(f"Subscribers: {metrics['subscribers']:,}")
    print(f"Avg view duration: {metrics['avg_view_duration_formatted']}")

if __name__ == "__main__":
    main()
```

---

## Validation Test

Run this before deploying to confirm the pipeline works end-to-end:

```python
# python scripts/validate_intake.py
from dashboard_intake import fetch_youtube_data, map_api_to_intake_metrics

def validate(creator_id: int = 19):
    raw = fetch_youtube_data(creator_id)
    metrics = map_api_to_intake_metrics(raw, window_days=30)

    required = [
        "subscribers", "conservative_avg_views", "avg_view_duration_formatted",
        "views_in_window", "watch_time_hours", "engagement_rate",
        "demo_top_age", "demo_male_pct", "demo_top_countries"
    ]

    missing = [f for f in required if metrics.get(f) is None]
    if missing:
        print(f"FAIL — missing: {missing}")
    else:
        print(f"PASS — all required fields present")
        print(f"  Creator: {metrics['channel_title']}")
        print(f"  Subscribers: {metrics['subscribers']:,}")
        print(f"  Conservative avg: {metrics['conservative_avg_views']:,}")
        print(f"  Avg duration: {metrics['avg_view_duration_formatted']}")
        print(f"  Demographics: {metrics['demo_top_age']} | {metrics['demo_male_pct']}% male | {metrics['demo_top_countries']}")

validate()
```

---

## Future Dashboard Updates (nice to have — already in the DB)

These fields are already stored in the database — they just need to be added to `app/routes/api.py`:

| Field | Table | Priority |
|---|---|---|
| Avg % viewed per video | `youtube_video_analytics` | High |
| Impressions + CTR | `youtube_traffic_sources` | Medium |
| Returning vs new viewer % | `youtube_demographics` | Medium |
| Traffic source breakdown | `youtube_traffic_sources` | Low |
| Daily subscriber gain/loss | `youtube_stats` | Low |

---

## Files Summary

| File | Action |
|---|---|
| `scripts/dashboard_intake.py` | **Create** — API fetch + metrics mapping |
| `scripts/intake_runner.py` | **Create** — entry point / CLI wrapper |
| `scripts/validate_intake.py` | **Create** — validation test |
| `SKILL.md` Step 1 | **Update** — replace Step 1A/1B with new priority order |
| `references/claude-code-handoff.md` | **Update** — add section: when source == 'elusive-dashboard-api', skip CSV parsing |
