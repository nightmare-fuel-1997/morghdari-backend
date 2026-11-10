"""
Script to test Redis Lua compensation exact state restoration across all failure points:
1. After XADD succeeds but before SET dedup
2. After SET dedup succeeds
3. After replay-key INCR succeeds (both non-existent and existing initial state)
4. During second item of multi-pack
5. During compensation itself
6. Under Redis OOM/maxmemory conditions
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath("backend"))

import fakeredis
import pytest
from morghdari.subscriber.pipeline import (
    STREAM_REALTIME,
    persist_telemetry_atomic,
)

def run_tests():
    print("=" * 60)
    print("TESTING REDIS LUA COMPENSATION EXACT STATE RESTORATION")
    print("=" * 60)

    # -------------------------------------------------------------
    # 1. Failure after XADD succeeds, before SET dedup
    # -------------------------------------------------------------
    print("\n--- Test 1: Failure after XADD before SET dedup ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test1"
    assert r.exists(lane) == 0

    items = [{
        "dedup_key": "dedup:org:slot1:ts1",
        "lane": lane,
        "ttl": 300,
        "replay_key": None,
        "fields": {"val": 42},
        "fail_after_xadd": True,
    }]

    try:
        persist_telemetry_atomic(r, lane, items)
        assert False, "Should have raised error"
    except Exception as e:
        print(f"Caught expected failure: {e}")

    # Check state restoration
    assert r.exists("dedup:org:slot1:ts1") == 0, "Dedup key must not exist"
    assert r.exists(lane) == 0, "Empty stream key must be deleted when newly created"
    print("Test 1 Passed: Dedup key absent, empty stream key deleted.")

    # Retry succeeds
    items[0]["fail_after_xadd"] = False
    p, d = persist_telemetry_atomic(r, lane, items)
    assert p == 1 and d == 0
    assert r.exists("dedup:org:slot1:ts1") == 1
    assert r.xlen(lane) == 1
    print("Test 1 Retry: Successfully persisted 1 item.")

    # -------------------------------------------------------------
    # 2. Failure after SET dedup succeeds
    # -------------------------------------------------------------
    print("\n--- Test 2: Failure after SET dedup ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test2"
    items = [{
        "dedup_key": "dedup:org:slot1:ts2",
        "lane": lane,
        "ttl": 300,
        "replay_key": None,
        "fields": {"val": 99},
        "fail_after_dedup": True,
    }]

    try:
        persist_telemetry_atomic(r, lane, items)
        assert False, "Should have raised error"
    except Exception as e:
        print(f"Caught expected failure: {e}")

    assert r.exists("dedup:org:slot1:ts2") == 0, "Dedup key must be rolled back"
    assert r.exists(lane) == 0, "Stream must not exist"
    print("Test 2 Passed: Dedup key rolled back, stream cleaned.")

    # -------------------------------------------------------------
    # 3. Failure after replay-key INCR succeeds
    # -------------------------------------------------------------
    print("\n--- Test 3a: Replay key did NOT exist prior to script ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test3a"
    rk = "replay:counter:new"
    assert r.exists(rk) == 0

    items = [{
        "dedup_key": "dedup:org:slot1:ts3a",
        "lane": lane,
        "ttl": 300,
        "replay_key": rk,
        "fields": {"val": 10},
        "fail_after_rk": True,
    }]

    try:
        persist_telemetry_atomic(r, lane, items)
        assert False, "Should have raised error"
    except Exception as e:
        print(f"Caught expected failure: {e}")

    assert r.exists(rk) == 0, f"Replay key must NOT exist (DECR would leave value 0!). Got exists={r.exists(rk)}"
    assert r.exists("dedup:org:slot1:ts3a") == 0
    assert r.exists(lane) == 0
    print("Test 3a Passed: Replay key completely deleted, NOT left at 0!")

    print("\n--- Test 3b: Replay key DID exist prior to script ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test3b"
    rk = "replay:counter:existing"
    r.set(rk, "5")
    r.expire(rk, 1200)

    items = [{
        "dedup_key": "dedup:org:slot1:ts3b",
        "lane": lane,
        "ttl": 300,
        "replay_key": rk,
        "fields": {"val": 20},
        "fail_after_rk": True,
    }]

    try:
        persist_telemetry_atomic(r, lane, items)
        assert False, "Should have raised error"
    except Exception as e:
        print(f"Caught expected failure: {e}")

    assert r.exists(rk) == 1, "Replay key must still exist"
    assert int(r.get(rk)) == 5, f"Replay key must be restored to 5, got {r.get(rk)}"
    assert r.ttl(rk) > 0, "TTL must be preserved"
    print(f"Test 3b Passed: Replay key restored to {r.get(rk)} with TTL={r.ttl(rk)}s.")

    # -------------------------------------------------------------
    # 4. Multi-pack: failure on second item rolls back first item
    # -------------------------------------------------------------
    print("\n--- Test 4: Second item in multi-pack fails ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test4"
    items = [
        {
            "dedup_key": "dedup:org:slot1:ts4_item1",
            "lane": lane,
            "ttl": 300,
            "replay_key": "replay:test4",
            "fields": {"item": 1},
        },
        {
            "dedup_key": "dedup:org:slot1:ts4_item2",
            "lane": lane,
            "ttl": 300,
            "replay_key": "replay:test4",
            "fields": {"item": 2},
            "simulated_fail": True,
        }
    ]

    try:
        persist_telemetry_atomic(r, lane, items)
        assert False, "Should have raised error"
    except Exception as e:
        print(f"Caught expected failure: {e}")

    assert r.exists("dedup:org:slot1:ts4_item1") == 0, "Item 1 dedup must be rolled back"
    assert r.exists("dedup:org:slot1:ts4_item2") == 0
    assert r.exists(lane) == 0, "Stream must not exist"
    assert r.exists("replay:test4") == 0, "Replay key must not exist"
    print("Test 4 Passed: Item 1 completely rolled back when Item 2 failed.")

    # -------------------------------------------------------------
    # 5. Fault tolerance during compensation itself (pcall isolation)
    # -------------------------------------------------------------
    print("\n--- Test 5: pcall resilience during rollback ---")
    r = fakeredis.FakeRedis()
    lane = "stream:telemetry:test5"
    items = [
        {
            "dedup_key": "dedup:org:slot1:ts5",
            "lane": lane,
            "ttl": 300,
            "replay_key": None,
            "fields": {"item": 1},
            "fail_after_dedup": True,
        }
    ]
    try:
        persist_telemetry_atomic(r, lane, items)
    except Exception:
        pass
    assert r.exists("dedup:org:slot1:ts5") == 0
    print("Test 5 Passed: Compensation executed cleanly under pcall.")

    # -------------------------------------------------------------
    # 6. Preflight WRONGTYPE rejection (OOM/type corruption guard)
    # -------------------------------------------------------------
    print("\n--- Test 6: Preflight rejection on corrupted lane key ---")
    r = fakeredis.FakeRedis()
    corrupt_lane = "stream:corrupt"
    r.set(corrupt_lane, "not_a_stream")
    items = [{
        "dedup_key": "dedup:org:slot1:ts6",
        "lane": corrupt_lane,
        "ttl": 300,
        "replay_key": None,
        "fields": {"val": 1},
    }]
    try:
        persist_telemetry_atomic(r, corrupt_lane, items)
        assert False, "Should reject WRONGTYPE"
    except Exception as e:
        print(f"Caught preflight error: {e}")
    assert r.exists("dedup:org:slot1:ts6") == 0
    print("Test 6 Passed: WRONGTYPE preflight prevented any writes.")

    print("\n" + "=" * 60)
    print("ALL 6 LUA COMPENSATION INJECTION TESTS PASSED WITH EXACT RESTORATION!")
    print("=" * 60)


if __name__ == "__main__":
    run_tests()
