# Repo Structure Recommendation
**From:** Claude (Cowork)
**For:** Claude Code — act on this before committing the 28 March 2026 session changes
**Date:** 28 March 2026

---

## Current State (what exists right now)

| Repo | What it actually contains |
|---|---|
| `elusive-dashboard` | FastAPI web app (deployed to Railway) + the new intake pipeline scripts in `scripts/` |
| `creator-qualify` | Everything else: `data_intake.py`, `data_intake_v2_preview.py`, all pitch scripts, skills, deck builder — the entire local pipeline toolkit |

The `intake_runner.py` bridges these two repos at runtime via a hardcoded `sys.path` insert pointing at `C:\Users\jjsem\OneDrive\Desktop\Cowork\Elusive\Templates`. This works locally but is fragile — one path change breaks the entire pipeline.

---

## The Core Question

Should the three intake scripts (`dashboard_intake.py`, `intake_runner.py`, `validate_intake.py`) stay in `elusive-dashboard/scripts/` or move to `creator-qualify` (Elusive/Templates)?

**Answer: Move them to `creator-qualify` (Elusive/Templates).**

Here's why.

---

## Reasoning

### The right boundary between the two repos

`elusive-dashboard` is a **deployed web application**. Its `scripts/` folder is for maintenance utilities that support the app itself — `diagnose_deepdive.py` is a good example, it debugs a live app feature. The new intake scripts are not web app utilities. They're a local pipeline tool that calls the app's API.

`creator-qualify` (Elusive/Templates) is the **local pipeline toolkit** — it's where `data_intake.py`, `data_intake_v2_preview.py`, and all the pitch scripts live. The intake runner is logically part of this toolkit. It belongs here.

Putting `intake_runner.py` in `elusive-dashboard/scripts/` and having it `sys.path` import from `creator-qualify` is the wrong way around. The dashboard is a dependency of the intake pipeline (it provides the API), not the other way.

### What the move looks like

Move these three files from `elusive-dashboard/scripts/` to `Elusive/Templates/scripts/` (or `Elusive/Templates/` root if scripts/ doesn't exist there):

```
FROM: elusive-dashboard/scripts/dashboard_intake.py
TO:   Elusive/Templates/scripts/dashboard_intake.py

FROM: elusive-dashboard/scripts/intake_runner.py
TO:   Elusive/Templates/scripts/intake_runner.py

FROM: elusive-dashboard/scripts/validate_intake.py
TO:   Elusive/Templates/scripts/validate_intake.py
```

After moving, the `sys.path` bootstrap in `intake_runner.py` is no longer needed — `data_intake.py` and `data_intake_v2_preview.py` are in the same directory. Replace the bootstrap block with a simple relative import or just remove it entirely since everything's co-located.

The `TEMPLATES_DIR` hardcoded path goes away completely.

---

## Should `creator-qualify` be renamed?

**Yes, eventually — but not right now.**

The repo name `creator-qualify` is misleading (it contains the entire pipeline toolkit, not just the qualifier). A better name would be `elusive-pipeline` or `elusive-toolkit`. However:

- Renaming a GitHub repo changes the remote URL and breaks any existing `git remote` configs on Joey's machine
- It's a cosmetic fix that doesn't unblock anything in the current sprint
- Do it in a dedicated session when there's nothing else in flight

For now, just note in the repo's README that `creator-qualify` is the legacy name and the repo contains the full Elusive pipeline toolkit.

---

## What to commit where, right now

### In `elusive-dashboard` repo — commit these:

```bash
git add app/api.py          # API expansion — needs to deploy to Railway
git add HANDOFF.md          # Updated with intake pipeline section
git add INTAKE-PIPELINE-HANDOFF.md   # New spec doc
git add CLAUDE_CODE_SESSION_HANDOFF_28MAR2026.md  # Session handoff
git add REPO-STRUCTURE-RECOMMENDATION.md  # This file
git commit -m "feat: expand /api/creators/{id}/youtube with full analytics fields

- Add avg_pct_viewed and avg_view_duration_seconds per video
- Add subscribers_gained/lost to daily_stats
- Add returning_viewer_pct to creator summary
- Add traffic_sources array
- Remove .limit(10) on videos query — intake needs full history
- Update handoff docs and add intake pipeline spec"
```

**Do NOT commit the three intake scripts to `elusive-dashboard`** — they're moving.

### In `creator-qualify` repo — commit these:

```bash
# First: move the scripts from elusive-dashboard/scripts/ to Elusive/Templates/scripts/
# (or wherever scripts live in that repo — check first)

git add scripts/dashboard_intake.py
git add scripts/intake_runner.py
git add scripts/validate_intake.py
git add data_intake.py            # DASHBOARD_CREATORS map + adapter + None guard fixes
git add data_intake_v2_preview.py # Page 2 + None guards
git add skills/elusive-data-intake.md  # Step 1 rewrite
git commit -m "feat: wire dashboard API into intake pipeline end-to-end

- Add dashboard_intake.py — API fetch, creator resolution, metrics mapping
- Add intake_runner.py — full CLI entry point (Steps 3-8 wired)
- Add validate_intake.py — roster validation tool
- Update data_intake.py with DASHBOARD_CREATORS map and API adapter
- Update data_intake_v2_preview.py with Page 2 and None guards
- Rewrite elusive-data-intake.md Step 1 with API-first priority"
```

---

## After Committing — Deploy the API Expansion

The `app/api.py` changes need to go live before the intake runner produces accurate data. After committing and pushing `elusive-dashboard`:

1. Railway auto-deploys on push to `main`
2. Run `validate_intake.py --all` after deploy to confirm the new fields are returning
3. Specifically check that `avg_pct_viewed` is non-null for creators who have `YouTubeVideoAnalytics` rows populated

---

## Summary

| Action | Repo | Priority |
|---|---|---|
| Commit `app/api.py` expansion + docs | `elusive-dashboard` | Do first — needs to deploy |
| Move 3 intake scripts + remove sys.path bootstrap | `elusive-dashboard` → `creator-qualify` | Do before committing scripts |
| Commit moved scripts + `data_intake.py` changes | `creator-qualify` | After move |
| Rename repo `creator-qualify` → `elusive-pipeline` | GitHub | Later — separate session |
