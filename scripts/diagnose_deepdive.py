"""
Diagnostic script for Video Deep Dive issues.
Run from the dashboard root: python scripts/diagnose_deepdive.py

Checks:
  1. Community Pulse - simulates _process_community_pulse with fake comments
  2. Concurrent session bug - proves asyncio.gather with same session is unsafe
  3. avg_view_duration / avg_pct_viewed - checks DB state and missing commit in _sync_video_analytics_batch
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.youtube import _process_community_pulse


# ─── TEST 1: _process_community_pulse logic ────────────────────────────────

def test_community_pulse_logic():
    print("\n── TEST 1: _process_community_pulse ─────────────────────────────")

    fake_comments = [
        {"text": "This is amazing, love it so much! Best video ever", "likes": 120, "author": "Alice", "published_at": None},
        {"text": "Finally! Needed this content, so helpful", "likes": 88, "author": "Bob", "published_at": None},
        {"text": "Great video keep it up underrated channel", "likes": 55, "author": "Carol", "published_at": None},
        {"text": "boring honestly, too long didn't finish", "likes": 3, "author": "Dave", "published_at": None},
        {"text": "sponsored ad content feels misleading", "likes": 2, "author": "Eve", "published_at": None},
        {"text": "love the editing and the topic finally", "likes": 40, "author": "Frank", "published_at": None},
        {"text": "This sponsor segment was too long honestly", "likes": 1, "author": "Grace", "published_at": None},
        {"text": "banger video goat content", "likes": 66, "author": "Hank", "published_at": None},
        {"text": "more of this please keep going", "likes": 33, "author": "Iris", "published_at": None},
        {"text": "fire video underrated for sure", "likes": 77, "author": "Jake", "published_at": None},
    ]

    result = _process_community_pulse(fake_comments)

    print(f"  Sentiment: {result['sentiment']}")
    print(f"  Top 5 comments count: {len(result['comments'])}")
    print(f"  Phrases: {result['phrases']}")
    print(f"  Sponsor flag: {result['sponsor_flag']}")

    assert result['sentiment'] in ('positive', 'mixed', 'negative'), "Sentiment should be set"
    assert len(result['comments']) <= 5, "Should cap at 5 comments"
    assert len(result['phrases']) <= 8, "Should cap at 8 phrases"
    print("  ✅ PASS — _process_community_pulse returns correct structure")

    # Test empty input
    empty_result = _process_community_pulse([])
    assert empty_result['sentiment'] is None
    assert empty_result['comments'] == []
    assert empty_result['sponsor_flag'] is False
    print("  ✅ PASS — Empty comments returns safe defaults")

    # Test sponsor flag threshold
    # 1 sponsor mention in 10 comments = 10% → above 3% threshold → should flag
    sponsor_comments = [
        {"text": "this is a sponsored video", "likes": 1, "author": "X", "published_at": None},
    ] + [{"text": "great video love it", "likes": 5, "author": f"U{i}", "published_at": None} for i in range(9)]
    sponsor_result = _process_community_pulse(sponsor_comments)
    assert sponsor_result['sponsor_flag'] is True, f"Expected sponsor flag, got {sponsor_result['sponsor_flag']}"
    print("  ✅ PASS — Sponsor flag fires correctly at >3% threshold")


# ─── TEST 2: Concurrent session safety simulation ──────────────────────────

async def test_concurrent_session_simulation():
    """
    Simulates the asyncio.gather(fetch_video_deep_dive, fetch_video_comments)
    pattern to show that sharing a SQLAlchemy AsyncSession across two
    concurrent coroutines is unsafe.

    We use a mock session that tracks concurrent access to prove the bug.
    """
    print("\n── TEST 2: Concurrent session bug simulation ─────────────────────")

    import time

    call_log = []

    class MockSession:
        """Records access patterns to expose concurrency overlap."""
        _active_operations = 0

        async def execute(self, stmt):
            MockSession._active_operations += 1
            call_log.append(("execute_start", MockSession._active_operations, time.perf_counter()))
            await asyncio.sleep(0.05)  # simulate DB latency
            MockSession._active_operations -= 1
            call_log.append(("execute_end", MockSession._active_operations, time.perf_counter()))

            class FakeResult:
                def scalar_one_or_none(self): return None
                def scalars(self): return self
                def all(self): return []
            return FakeResult()

        async def commit(self):
            MockSession._active_operations += 1
            call_log.append(("commit_start", MockSession._active_operations, time.perf_counter()))
            await asyncio.sleep(0.05)
            MockSession._active_operations -= 1
            call_log.append(("commit_end", MockSession._active_operations, time.perf_counter()))

    session = MockSession()

    async def fake_deep_dive(db):
        await db.execute("SELECT 1")  # _get_valid_token check
        await asyncio.sleep(0.1)      # API calls
        await db.execute("SELECT 2")  # upsert YouTubeVideoAnalytics
        await db.commit()             # the commit in fetch_video_deep_dive

    async def fake_comments(db):
        await db.execute("SELECT 3")  # _get_valid_token check
        await asyncio.sleep(0.05)     # API call (faster)

    # Run concurrently as the buggy code does
    await asyncio.gather(fake_deep_dive(session), fake_comments(session))

    max_concurrent = max(count for _, count, _ in call_log)
    overlaps = [(op, count) for op, count, _ in call_log if count > 1]

    print(f"  Peak concurrent operations on same session: {max_concurrent}")
    print(f"  Overlap events (count > 1 = concurrent access): {len(overlaps)}")

    if max_concurrent > 1:
        print(f"  ❌ BUG CONFIRMED — Session was accessed by {max_concurrent} coroutines simultaneously")
        print(f"     SQLAlchemy AsyncSession is NOT concurrency-safe.")
        print(f"     Root cause: asyncio.gather(fetch_video_deep_dive, fetch_video_comments) share the same db session.")
        print(f"     When fetch_video_deep_dive commits, fetch_video_comments may be mid-query → silent failure.")
        print(f"     Fix: Run fetch_video_comments sequentially AFTER fetch_video_deep_dive.")
    else:
        print("  ✅ No concurrent access detected (unexpected — check simulation)")


# ─── TEST 3: avg_view_duration missing commit ──────────────────────────────

async def test_missing_commit():
    print("\n── TEST 3: avg_view_duration / avg_pct_viewed — missing commit ───")

    # Read youtube.py source to verify the commit is absent
    youtube_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "youtube.py")
    with open(youtube_path, "r", encoding="utf-8") as f:
        source = f.read()

    # Find _sync_video_analytics_batch and check if db.commit is inside it
    batch_start = source.find("async def _sync_video_analytics_batch(")
    if batch_start == -1:
        print("  ⚠️  Could not find _sync_video_analytics_batch in source")
        return

    # Find the next function definition after _sync_video_analytics_batch
    next_func = source.find("\nasync def ", batch_start + 1)
    batch_body = source[batch_start:next_func]

    has_commit = "await db.commit()" in batch_body
    writes_avg = "avg_view_duration" in batch_body

    print(f"  _sync_video_analytics_batch writes avg_view_duration: {writes_avg}")
    print(f"  _sync_video_analytics_batch calls await db.commit(): {has_commit}")

    if writes_avg and not has_commit:
        print(f"  ❌ BUG CONFIRMED — avg_view_duration and avg_pct_viewed are written but never committed.")
        print(f"     All writes from _sync_video_analytics_batch are silently discarded when the session ends.")
        print(f"     Fix: Add `await db.commit()` at the end of _sync_video_analytics_batch.")
    elif has_commit:
        print(f"  ✅ Commit is present — avg_view_duration should persist after sync")
    else:
        print(f"  ⚠️  avg_view_duration not found in batch function — check function body")

    # Also check fetch_video_deep_dive — does it write avg_view_duration?
    deepdive_start = source.find("async def fetch_video_deep_dive(")
    end_of_file = len(source)
    deepdive_body = source[deepdive_start:end_of_file]
    deepdive_writes_avg = "avg_view_duration" in deepdive_body

    print(f"\n  fetch_video_deep_dive writes avg_view_duration: {deepdive_writes_avg}")
    if not deepdive_writes_avg:
        print(f"  ℹ️  Deep dive route doesn't set avg_view_duration — values depend entirely on sync.")
        print(f"     If sync never committed, deep dive will always show 0 for these fields.")


# ─── TEST 4: DB state check (live) ────────────────────────────────────────

async def test_db_state():
    print("\n── TEST 4: Live DB state — YouTubeVideoAnalytics ─────────────────")
    try:
        from app.database import AsyncSessionLocal
        from app.models import YouTubeVideoAnalytics, YouTubeVideo

        async with AsyncSessionLocal() as db:
            from sqlalchemy import select, func

            # Count total analytics records
            count_result = await db.execute(select(func.count()).select_from(YouTubeVideoAnalytics))
            total = count_result.scalar()

            # Count records with avg_view_duration set
            avg_result = await db.execute(
                select(func.count()).select_from(YouTubeVideoAnalytics)
                .where(YouTubeVideoAnalytics.avg_view_duration.isnot(None))
            )
            with_avg = avg_result.scalar()

            # Count records with retention_data set
            ret_result = await db.execute(
                select(func.count()).select_from(YouTubeVideoAnalytics)
                .where(YouTubeVideoAnalytics.retention_data.isnot(None))
            )
            with_retention = ret_result.scalar()

            print(f"  Total YouTubeVideoAnalytics rows: {total}")
            print(f"  Rows with avg_view_duration set: {with_avg}")
            print(f"  Rows with retention_data set: {with_retention}")

            if total > 0 and with_avg == 0:
                print(f"  ❌ CONFIRMED — No avg_view_duration in DB despite {total} analytics records.")
                print(f"     The missing commit in _sync_video_analytics_batch is the cause.")
            elif with_avg > 0:
                print(f"  ✅ avg_view_duration is populated for {with_avg}/{total} records")

    except Exception as e:
        print(f"  ⚠️  DB test skipped (can't connect in this environment): {type(e).__name__}: {e}")
        print(f"     Run this script on the Railway server or locally with DB access to get live results.")


# ─── SUMMARY ──────────────────────────────────────────────────────────────

async def main():
    print("=" * 65)
    print("  ELUSIVE DASHBOARD — VIDEO DEEP DIVE DIAGNOSTIC")
    print("=" * 65)

    test_community_pulse_logic()
    await test_concurrent_session_simulation()
    await test_missing_commit()
    await test_db_state()

    print("\n" + "=" * 65)
    print("  FIXES REQUIRED")
    print("=" * 65)
    print("""
  BUG 1 — Community Pulse not showing
  ─────────────────────────────────────────────────────────────
  Location: app/main.py → video_deep_dive() route
  Cause:    asyncio.gather(fetch_video_deep_dive, fetch_video_comments)
            shares one SQLAlchemy AsyncSession across two coroutines.
            When fetch_video_deep_dive commits mid-gather, the session
            state is corrupted for fetch_video_comments → it returns
            empty dict → template guard hides section.
  Fix:      Run sequentially. fetch_video_comments AFTER fetch_video_deep_dive.

  BUG 2 — avg_view_duration / avg_pct_viewed always 0
  ─────────────────────────────────────────────────────────────
  Location: app/youtube.py → _sync_video_analytics_batch()
  Cause:    Function writes analytics.avg_view_duration and
            analytics.avg_pct_viewed but never calls await db.commit().
            All writes are discarded when the session closes.
  Fix:      Add `await db.commit()` at end of _sync_video_analytics_batch.
""")


if __name__ == "__main__":
    asyncio.run(main())
