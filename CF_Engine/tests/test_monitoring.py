"""tests/test_monitoring.py — 실행 자동화·모니터링 유틸 (P2).

재시도·구조화 로그·지표 변화 계산을 DB 없이 검증한다. sleep 을 주입해 실제 대기 없음.
"""

import sys

import pytest

sys.path.insert(0, ".")

from CF_Engine.src.monitoring import (
    run_with_retry,
    build_run_record,
    append_run_log,
    read_run_log,
    metrics_delta,
    last_success,
)


# ── 재시도 ───────────────────────────────────────────────────────────

def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temp")
        return "ok"

    result = run_with_retry(flaky, retries=5, backoff_sec=0, sleep=lambda s: None)
    assert result == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_raises():
    attempts = []

    def always_fail():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_with_retry(always_fail, retries=2, backoff_sec=0,
                       sleep=lambda s: None,
                       on_error=lambda a, e: attempts.append(a))
    assert attempts == [1, 2, 3]       # 최초 + 2회 재시도


def test_retry_no_sleep_on_first_success():
    slept = []
    run_with_retry(lambda: 42, retries=3, backoff_sec=10,
                   sleep=lambda s: slept.append(s))
    assert slept == []                 # 성공 시 대기 없음


# ── 레코드/로그 ──────────────────────────────────────────────────────

def test_build_run_record_elapsed():
    rec = build_run_record("cf_train", "success",
                           "2026-11-12T00:00:00+00:00",
                           "2026-11-12T00:01:30+00:00",
                           stats={"records": 100})
    assert rec["status"] == "success"
    assert rec["elapsed_sec"] == pytest.approx(90.0)
    assert rec["stats"]["records"] == 100


def test_append_and_read_jsonl_roundtrip(tmp_path):
    path = tmp_path / "run_log.jsonl"
    r1 = build_run_record("cf_train", "success", "2026-11-12T00:00:00+00:00",
                          "2026-11-12T00:00:10+00:00", stats={"inserted": 10})
    r2 = build_run_record("cf_train", "failed", "2026-11-12T01:00:00+00:00",
                          "2026-11-12T01:00:05+00:00", error="db down")
    append_run_log(path, r1)
    append_run_log(path, r2)
    recs = read_run_log(path)
    assert len(recs) == 2
    assert recs[0]["stats"]["inserted"] == 10
    assert recs[1]["status"] == "failed"
    assert recs[1]["error"] == "db down"


def test_read_missing_log_is_empty(tmp_path):
    assert read_run_log(tmp_path / "nope.jsonl") == []


# ── 지표 변화 ────────────────────────────────────────────────────────

def test_metrics_delta():
    prev = {"records": 100, "inserted": 100}
    curr = {"records": 120, "inserted": 118}
    d = metrics_delta(prev, curr)
    assert d["records"] == 20
    assert d["inserted"] == 18


def test_metrics_delta_no_prev():
    d = metrics_delta(None, {"records": 50})
    assert d["records"] is None


def test_last_success_picks_latest():
    recs = [
        {"job": "cf_train", "status": "success", "stats": {"v": 1}},
        {"job": "cf_train", "status": "failed"},
        {"job": "cf_train", "status": "success", "stats": {"v": 2}},
        {"job": "cf_train", "status": "failed"},
    ]
    s = last_success(recs, job="cf_train")
    assert s["stats"]["v"] == 2         # 가장 최근 성공

    assert last_success([], job="cf_train") is None
