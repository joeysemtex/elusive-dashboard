# Claude Code Session Handoff
**Date:** 28 March 2026
**Prepared by:** Claude (Cowork)
**For:** Next Claude Code session

---

## Session Summary

This document consolidates everything from the 28 March 2026 Cowork + Claude Code session. Claude Code completed part of the work; the remainder is scoped here for the next Claude Code session.

---

## What Claude Code Already Completed (do not redo)

### 1. Dashboard API intake scripts — `elusive-dashboard/scripts/`

Three new scripts are live in the repo:

- **`scripts/dashboard_intake.py`** — Core library. Fetches from `/api/creators` and `/api/creators/{id}/youtube`, resolves creator by name (fuzzy match), checks data freshness (warn >24h, hard fail >7d), maps the API response to the standard intake metrics dict. All gap fields (impressions, CTR, etc.) explicitly set to `None` with comments.

- **`scripts/intake_runner.py`** — Full CLI runner. Args: `--creator`, `--window` (30/60/90), `--purpose` (pitch/health_check/brand_pitch), `--brand`. Hard exits on brand_pitch without brand name. Prints a full metric summary. Currently ends after the summary — Steps 3–8 are not yet wired in (see Outstanding below).

- **`scripts/validate_intake.py`** — CLI validation tool. `python scripts/validate_intake.py` tests Rain Nebula (id=19). `--id N` tests a specific creator. `--all` runs the full roster and prints a pass/fail table. Exits with code 1 on any failure.

No new dependencies were added — `httpx` and `python-dotenv` were already in `requirements.txt`.

**These scripts are not yet committed to git.**

### 2. `HANDOFF.md` updated

The intake pipeline integration section has been added to `elusive-dashboard/HANDOFF.md`, including the outstanding wiring task, the source flag note, and the missing API fields list.

### 3. `INTAKE-PIPELINE-HANDOFF.md` created

Full spec document at `elusive-dashboard/INTAKE-PIPELINE-HANDOFF.md`. Contains the complete data source priority order, full Python implementation of `map_api_to_intake_metrics()`, AskUserQuestion flow design, and the future API improvements list. This is the reference document for all intake pipeline work.

### 4. `data_intake_v2_preview.py` — Page 2 added (separate task)

**File:** `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\data_intake_v2_preview.py`

Page 2 was added for supplementary platform analytics (IG + FB). Renders only when `ig_data` or `fb_data` is non-null. Contents: cross-platform hero band (combined reach), per-platform stat strip (6 metrics each), strengths/weaknesses block, footer with combined reach callout. Preview PDF was generated: `Longy TCA_DataIntake_v2_MAR2026.pdf`.

---

## Gotchas — Read Before Starting

These are not in the Claude Code completion report. They will bite you if you skip this section.

### 1. `intake_runner.py` hard exits on stale data (>7d) — not just a warning

The spec said warn on >24h and >7d. The implementation hard exits on >7d. If you need to test against a creator whose dashboard data is more than 7 days old (e.g. fog, who last synced 28 March 2026 and may be stale by the time you run this), you'll need to either:
- Trigger a manual sync first: `POST /admin/sync/{creator_id}` with the `X-Api-Key` header, or
- Temporarily comment out the hard-exit branch in `check_data_freshness()` for local testing only

Do not remove the hard-exit behaviour permanently — it's there to protect pitch quality.

### 2. Skill path mismatch — CSV fallback won't work from Cowork

`elusive-data-intake.md` Step 1B was updated by Claude Code to use Windows paths:
```
C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Talent\
```
That path resolves fine from Claude Code (Windows shell). It does **not** resolve from Cowork, which uses the mount path:
```
/sessions/[session-id]/mnt/Cowork/Elusive/Talent/
```
The API path (Step 1A) is network-based and works from both. Only the CSV fallback (Step 1B) has this issue. When wiring up the fallback path in the runner, use an environment-aware path resolution — check whether the Windows path exists; if not, fall back to a configurable base path or environment variable.

Suggested fix in `intake_runner.py`:
```python
import os
TALENT_BASE = os.getenv("TALENT_BASE", r"C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Talent")
```

### 3. `references/claude-code-handoff.md` does not exist — needs to be created

The original spec listed this as a file to update in the `elusive-dashboard` repo. Claude Code skipped it because the file didn't exist. It needs to be created as a new file at:
```
elusive-dashboard/references/claude-code-handoff.md
```

The only content it needs is this note (the rest of the handoff is covered by this file):

> When `metrics["source"] == "elusive-dashboard-api"`, skip all CSV parsing logic in Steps 2–3 of the intake pipeline. The metrics dict is already fully populated from the dashboard API. Proceed directly to analysis (Step 4 onwards). Gap fields (`impressions`, `impressions_ctr`, `avg_pct_viewed`, `returning_viewer_pct`, `traffic_sources`, `subscribers_gained`) will be `None` — handle gracefully in Step 5 weakness assessment and do not surface in pitch output.

### 4. `avg_pct_viewed` proxy — implement carefully

This field is `None` from the API. In Step 3 per-video analysis, use `avg_view_duration_seconds / duration_seconds * 100` as a proxy **only when both values are present and duration > 0**. Label it `"avg_pct_viewed_estimated"` (not `"avg_pct_viewed"`) in the output so downstream steps know it's derived, not sourced. Do not surface it in the pitch PDF without a note.

---

## Task Ownership

| Task | Who | Why |
|---|---|---|
| Wire Steps 3–8 into the runner | **Claude Code** | Script editing + file generation — same pattern as this session |
| API field expansion (`app/routes/api.py`) | **Claude Code** | Backend repo work, independent of the wiring task — can run in parallel |
| AskUserQuestion intake flow | **Cowork** | Conversational orchestration — questions happen before any script runs; Cowork passes answers as args to the runner |

**Sequencing:** Wire Steps 3–8 first so there's a functional end-to-end runner for Cowork to hand off to. API field expansion can happen in parallel — it's independent. Cowork picks up the AskUserQuestion flow once the runner produces a real PDF.

---

## Outstanding — What the Next Claude Code Session Should Do

### Priority 1: Wire Steps 3–8 into the intake runner

**What:** Connect `intake_runner.py` to the existing analysis, PDF generation, and config-population pipeline.

**Entry point** (already written — just needs to be called):
```python
from scripts.dashboard_intake import (
    get_all_creators, resolve_creator,
    fetch_youtube_data, check_data_freshness,
    map_api_to_intake_metrics
)
metrics = map_api_to_intake_metrics(fetch_youtube_data(creator_id), window_days=30)
# → hand metrics into existing Steps 3–8
```

**Critical rule to implement:** When `metrics["source"] == "elusive-dashboard-api"`, skip all CSV parsing logic. The dict is already populated — go straight to analysis (Step 4 of the data-intake skill).

**Steps 3–8 of the skill are:**
- Step 3: Per-video analysis (outlier detection, conservative average — already calculated in `map_api_to_intake_metrics`, just needs to flow through)
- Step 4: Channel-level metrics surface
- Step 5: Weakness assessment
- Step 6: Downstream readiness check
- Step 7: Rate card inputs
- Step 8: PDF output

**Note on avg % viewed:** This field is `None` from the API (not exposed yet). In Step 3, use `avg_view_duration_seconds / duration_seconds * 100` as a proxy where per-video duration is available. Flag it as "estimated" in the output.

---

### Priority 2: Implement the AskUserQuestion intake flow

**What:** When Joey says "do a data intake on [Creator]", the skill should now ask clarifying questions before proceeding. The questions are fully designed in `INTAKE-PIPELINE-HANDOFF.md` — reproduce them here for convenience:

```
Q1 (header: "Window") — always ask:
  "What time window should I analyse?"
  Options:
    - Last 30 days  ← recommended
    - Last 60 days
    - All available data

Q2 (header: "Purpose") — skip if clear from context:
  "What's this intake for?"
  Options:
    - Full pitch intake (PDF + auto-populate configs)  ← recommended
    - Quick health check (chat summary only, no PDF)
    - Specific brand pitch  ← follow up with brand name

Q3 (header: "Platforms") — only ask if creator has non-zero IG data:
  "Include Instagram?"
  Options:
    - YouTube only
    - YouTube + Instagram (from dashboard)
    - YouTube + Instagram + I'll provide extra IG data manually
```

This logic lives in the `elusive-data-intake` SKILL.md (Step 1A, updated by Claude Code this session). The runner should read the answers and pass them as args.

---

### Priority 3: Expose missing fields in the dashboard API

**What:** Five fields are already in the database but not returned by the API. They're worth adding to `app/api.py` before the next pitch cycle. Listed by priority:

| Field | Source table | Add to endpoint |
|---|---|---|
| Avg % viewed per video | `youtube_video_analytics` | `/api/creators/{id}/youtube` |
| Impressions + CTR | `youtube_traffic_sources` | `/api/creators/{id}/youtube` |
| Returning vs new viewer % | `youtube_demographics` | `/api/creators/{id}/youtube` |
| Traffic source breakdown | `youtube_traffic_sources` | `/api/creators/{id}/youtube` |
| Daily subscriber gain/loss | `youtube_stats` | `/api/creators/{id}/youtube` |

Once added, remove the `None` placeholders from `map_api_to_intake_metrics()` in `dashboard_intake.py` and map the real values.

---

### Priority 4: Commit the three new scripts

```bash
cd C:\Users\jjsem\OneDrive\Desktop\Cowork\elusive-dashboard
git add scripts/dashboard_intake.py scripts/intake_runner.py scripts/validate_intake.py
git add HANDOFF.md INTAKE-PIPELINE-HANDOFF.md CLAUDE_CODE_SESSION_HANDOFF_28MAR2026.md
git commit -m "Add dashboard API intake pipeline scripts + handoff docs"
```

---

## Current Roster (dashboard, as of 28 March 2026)

| Creator name | ID | Channel |
|---|---|---|
| Rain Nebula | 19 | Nebula CS2 |
| Vivienne Quach | 17 | BibiahnCS2 |
| Bonathan | 20 | BonathanCS |
| voocsgo | 21 | vooCSGO |
| 2voo | 23 | its voo |
| Riftlab | 24 | RiftlabTCG |
| Koady Boyd | 25 | Rival/RivalRVN |
| Nebula | 27 | nebulaisacoolguy |
| The Casual Athlete | 18 | NRL Fantasy |
| fog | 29 | New addition |

---

## API Reference

**Base URL:** `https://dashboard.elusivemgmt.com`
**Auth:** `X-Api-Key` header — value in `.env` as `PIPELINE_API_KEY`

| Endpoint | Returns |
|---|---|
| `GET /api/creators` | All creators + summary stats |
| `GET /api/creators/{id}/youtube` | Full YouTube data — channel, videos, daily stats, demographics, long-form/shorts split |
| `GET /api/creators/{id}/export` | Pitch-ready summary — top 5 videos + key metrics |
| `POST /admin/sync/{creator_id}` | Trigger a manual YouTube data refresh |

---

## File Locations

| File | Path |
|---|---|
| Core intake library | `elusive-dashboard/scripts/dashboard_intake.py` |
| CLI runner | `elusive-dashboard/scripts/intake_runner.py` |
| Validation test | `elusive-dashboard/scripts/validate_intake.py` |
| Intake pipeline spec | `elusive-dashboard/INTAKE-PIPELINE-HANDOFF.md` |
| Dashboard build handoff | `elusive-dashboard/HANDOFF.md` |
| Data intake skill | `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\skills\elusive-data-intake.md` |
| v2 preview script | `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates\data_intake_v2_preview.py` |
