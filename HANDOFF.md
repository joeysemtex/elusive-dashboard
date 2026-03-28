# Elusive Analytics Dashboard — Build Handoff

**From:** Claude Code (Phase 1 build session)
**To:** Claude Cowork (ongoing maintenance + deployment assist)
**Date:** 2026-03-24

---

## What Was Built

Phase 1 of the Elusive Analytics Dashboard is complete and pushed to `github.com/joeysemtex/elusive-dashboard` on `main`. The app is a standalone FastAPI web application that aggregates YouTube creator stats into an agency-wide dashboard for Joey and individual creator dashboards for each Elusive talent.

### Repo Structure

```
elusive-dashboard/
  app/
    __init__.py
    main.py           # FastAPI app, all routes, template rendering, admin endpoints
    auth.py            # Google OAuth 2.0 via Authlib (login + YouTube token acquisition)
    api.py             # Pipeline REST API for Cowork/Claude Code integration
    config.py          # Settings from environment variables
    crypto.py          # Fernet encryption/decryption for OAuth tokens at rest
    database.py        # SQLAlchemy async engine + session factory
    models.py          # User, Creator, YouTubeStat, YouTubeVideo, YouTubeDemographic
    scheduler.py       # APScheduler — 6-hour YouTube refresh cycle
    youtube.py         # YouTube Data API v3 + Analytics API client (full sync logic)
  templates/
    base.html          # Sidebar layout, nav, user info — all pages extend this
    login.html         # Standalone login page (Google OAuth button)
    agency.html        # Agency dashboard — creator grid, aggregate metrics, sparklines
    creator.html       # Creator dashboard — hero metrics, 30-day chart, videos, demographics
  static/
    css/styles.css     # Full design system — Section 4 tokens, responsive, dark sidebar
    img/default-avatar.svg
  alembic/             # Database migration scaffolding (env.py, script template)
  .env.example         # Template for all required environment variables
  .gitignore           # Excludes .env, credentials, dev.db, __pycache__, .claude/
  DECISIONS.md         # Spec deviations per Section 7 autonomy clause
  Procfile             # Railway start command
  railway.toml         # Railway build + deploy config
  requirements.txt     # All Python dependencies pinned
```

---

## Deviations from Original Spec

Per Section 7 (Autonomy Clause), the following changes were made. Full rationale in `DECISIONS.md`.

| Spec Recommendation | What Was Built | Why |
|---|---|---|
| React + Tailwind frontend | Jinja2 + HTMX + Chart.js | No build step, one deployment artifact, same interactivity at this scale |
| Tailwind CSS | Custom CSS | Exact control over Section 4 design tokens, no build toolchain |
| PostgreSQL | PostgreSQL (kept) | Railway containers are ephemeral — SQLite would lose data on redeploy |
| Auth approach unspecified | Authlib | Well-maintained, handles Google OAuth 2.0 + token refresh cleanly |
| APScheduler recommended | APScheduler (kept) | In-process, no external dependency needed |

**Nothing was dropped from the spec.** All Section 1 requirements are implemented. The deviations are purely implementation choices, not scope reductions.

---

## What the Spec Calls For vs Current State

### Phase 1 Deliverables (all complete)

- [x] FastAPI backend with Google OAuth flow
- [x] PostgreSQL database with encrypted token storage (Fernet)
- [x] YouTube Data API + Analytics API integration with 6-hour refresh
- [x] Agency dashboard: creator grid with metric cards and sparklines
- [x] Creator dashboard: full YouTube stats view with top videos
- [x] Admin role (Joey) and creator role with Google OAuth login
- [x] Railway deployment config (Procfile + railway.toml)
- [x] Pipeline API endpoints (`/api/creators`, `/api/creators/{id}/youtube`, `/api/creators/{id}/export`)

### Phase 2 (Instagram — blocked on Meta App Review)

The database models already have Instagram fields (`ig_followers`, `ig_reach_30d`, `ig_engagement_rate`, `instagram_access_token`, etc.). The OAuth redirect URI is configured in Meta's developer portal. When Meta approves the app, Phase 2 requires:

- Instagram OAuth flow in `app/auth.py` (alongside Google)
- Instagram Graph API client (new file `app/instagram.py`)
- Creator dashboard updated with Instagram section
- Agency dashboard shows Instagram metrics alongside YouTube
- Pipeline export endpoint includes Instagram data

### Phase 3 (Polish + Pipeline Deep Integration)

- Audience demographics panel enhancements (already partially built — age, gender, geo bars exist)
- Historical trend charts (30/60/90-day)
- Data-intake skill updated to query dashboard API instead of manual CSV
- Performance-monitor skill updated to pull from dashboard API
- Mobile-responsive refinement (responsive CSS already exists but needs device testing)
- TikTok placeholder UI

---

## Credentials and Secrets

**Location:** `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\`

| File | Contains |
|---|---|
| `google_oauth_credentials.json` | Google Client ID, Client Secret, project ID, redirect URI |
| `meta_oauth_credentials.json` | Meta App ID, App Secret, redirect URI (Phase 2) |

**These files are NOT in the repo.** Values must be extracted into Railway environment variables during deployment.

### Environment Variables Required

```
GOOGLE_CLIENT_ID=415456459983-3khcqs6l54cu3rijsbmpjgi3h1a1c0lr.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=<from google_oauth_credentials.json>
DATABASE_URL=<Railway auto-provisions when you add PostgreSQL>
SECRET_KEY=<generate: python -c "import secrets; print(secrets.token_hex(32))">
FERNET_KEY=<generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
BASE_URL=https://dashboard.elusivemgmt.com
PIPELINE_API_KEY=<generate: python -c "import secrets; print(secrets.token_hex(16))">
ADMIN_EMAIL=josephjsemaan@gmail.com
```

**Phase 2 additions (when Meta approves):**
```
META_APP_ID=1638783127022935
META_APP_SECRET=<from meta_oauth_credentials.json>
```

---

## Go-Live Steps for Joey

Walk Joey through these in order. Each step depends on the previous one.

### Step 1: Connect Railway to GitHub

1. Go to railway.app and log in
2. Click "New Project" > "Deploy from GitHub repo"
3. Select `joeysemtex/elusive-dashboard`
4. Railway will detect the Procfile and start building

### Step 2: Add PostgreSQL Database

1. In the Railway project, click "New" > "Database" > "PostgreSQL"
2. Railway auto-creates a `DATABASE_URL` variable and links it to the app
3. Verify the variable exists: go to the app service > Variables tab

### Step 3: Set Environment Variables

In Railway, go to the app service > Variables tab. Add each variable listed above. For the generated values, Joey can run the Python commands in his terminal or Cowork can generate them and provide the values.

**Critical:** `BASE_URL` should initially be set to the Railway-provided URL (e.g. `https://elusive-dashboard-production.up.railway.app`) for testing. Change it to `https://dashboard.elusivemgmt.com` after DNS is configured.

### Step 4: Deploy and Test

1. Railway auto-deploys after variables are set
2. Click "Generate Domain" in Railway to get a `*.up.railway.app` URL
3. Visit the URL — the login page should appear
4. Test Google login with Joey's admin email
5. After login, the agency dashboard should load (empty — no creators yet)

### Step 5: Google OAuth Redirect URI Update (if testing on Railway URL)

If testing on the Railway URL before DNS is set up, Joey needs to temporarily add the Railway URL as an authorised redirect URI in Google Cloud Console:

1. Go to console.cloud.google.com > Elusive Dashboard project > Credentials
2. Edit the OAuth client > Add `https://<railway-url>/auth/google/callback` to authorised redirect URIs
3. Save

Once DNS is live and `dashboard.elusivemgmt.com` resolves, the original redirect URI works and the Railway one can be removed.

### Step 6: DNS Configuration

1. In the domain registrar for elusivemgmt.com, add a CNAME record:
   - Host: `dashboard`
   - Value: the Railway-provided URL (without `https://`)
   - TTL: Auto or 3600
2. In Railway, go to Settings > Custom Domain > add `dashboard.elusivemgmt.com`
3. Railway handles SSL automatically
4. Update `BASE_URL` env var to `https://dashboard.elusivemgmt.com`
5. Redeploy

### Step 7: Upgrade Railway Plan

When everything is confirmed working, upgrade from the trial to the Hobby plan ($5/month) to keep the app running after the 30-day trial ends.

---

## Pipeline API for Cowork Integration

Once live, Cowork and Claude Code can query the dashboard API during pitch builds:

```
GET /api/creators
GET /api/creators/{id}/youtube
GET /api/creators/{id}/export
```

All endpoints require `X-Api-Key` header with the `PIPELINE_API_KEY` value. The `/export` endpoint returns pitch-ready JSON matching the data-intake schema.

---

## Intake Pipeline Integration (28 March 2026)

Three new scripts were added to `scripts/` by Claude Code to wire the dashboard API into the data-intake pipeline. See `INTAKE-PIPELINE-HANDOFF.md` for the full spec.

### Scripts added

| File | Status | Purpose |
|---|---|---|
| `scripts/dashboard_intake.py` | ✅ Complete | Core API library — fetch, resolve, freshness check, metrics mapping |
| `scripts/intake_runner.py` | ✅ Complete | CLI entry point — `--creator`, `--window`, `--purpose`, `--brand` args |
| `scripts/validate_intake.py` | ✅ Complete | Validation test — single creator or `--all` roster sweep |

### What's still outstanding (next Claude Code session)

**Steps 3–8 not yet wired up.** The `intake_runner.py` currently ends at a printed summary. The metrics dict is correctly shaped — it just needs to be handed into the existing analysis, PDF generation, and config-population steps. Entry point:

```python
from scripts.dashboard_intake import (
    get_all_creators, resolve_creator,
    fetch_youtube_data, check_data_freshness,
    map_api_to_intake_metrics
)
metrics = map_api_to_intake_metrics(fetch_youtube_data(creator_id), window_days=30)
# → hand metrics into existing Steps 3–8 of elusive-data-intake skill
```

**Source flag to respect:** When `metrics["source"] == "elusive-dashboard-api"`, skip all CSV parsing logic in the intake pipeline. The metrics dict is already fully populated — proceed directly to the analysis steps (Step 4 onwards in the skill).

**Path note:** The data-intake skill was updated to use Windows paths (`C:\Users\jjsem\...`). If Cowork is executing the CSV fallback path directly, use the Cowork mount path instead. The API path is network-based and works from either environment.

**Fields not yet in API (already in DB — low-effort addition to `app/api.py`):**

| Field | Table | Priority |
|---|---|---|
| Avg % viewed per video | `youtube_video_analytics` | High |
| Impressions + CTR | `youtube_traffic_sources` | Medium |
| Returning vs new viewer % | `youtube_demographics` | Medium |
| Traffic source breakdown | `youtube_traffic_sources` | Low |
| Daily subscriber gain/loss | `youtube_stats` | Low |

**Repo state:** All three scripts are in `scripts/`. No new dependencies (httpx + python-dotenv were already in `requirements.txt`). Files are not yet committed — stage and commit when ready.

---

## Known Considerations

1. **Google OAuth is in Testing mode** — max 100 users. Fine for the current roster. If Elusive grows past 100 creators, Joey submits for Google verification (2-4 weeks).

2. **The app creates tables on startup** via `init_db()`. No manual migration step needed for the initial deployment. Alembic scaffolding is in place for future schema changes.

3. **First creator login triggers an immediate YouTube sync** — the creator doesn't need to wait for the 6-hour cycle. Joey (admin) can also manually trigger syncs via the "Sync Now" button on each creator's dashboard.

4. **The `strftime` Jinja2 filter** was added as a custom filter in `app/main.py` (line ~97) since Jinja2 doesn't include it natively. If any template date formatting breaks, check that filter.

5. **Starlette TemplateResponse signature** — uses the newer `TemplateResponse(request, "template.html", context)` format. If downgrading Starlette, the old `TemplateResponse("template.html", {"request": request, ...})` format would be needed.

---

## File Locations Summary

| What | Where |
|---|---|
| Project code | `C:\Users\jjsem\OneDrive\Desktop\Cowork\elusive-dashboard\` |
| GitHub repo | `github.com/joeysemtex/elusive-dashboard` (main branch) |
| Google credentials | `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\google_oauth_credentials.json` |
| Meta credentials | `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\meta_oauth_credentials.json` |
| Original build spec | `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\Elusive_Analytics_Dashboard_Handoff_v2.pdf` |
| This handoff | `C:\Users\jjsem\OneDrive\Desktop\Cowork\elusive-dashboard\HANDOFF.md` |
