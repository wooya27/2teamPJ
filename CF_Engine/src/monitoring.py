"""CF_Engine 실행 자동화·모니터링 유틸(라이브러리).

정기 배치의 성공/실패, 처리 건수, 소요 시간, 모델 성능 변화를 구조화 로그(JSONL)로
남기고, 실패 시 안전한 재시도·알림을 지원한다. 순수 로직은 단위 테스트 가능하다.
"""

from __future__ import annotations

import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def run_with_retry(fn, retries: int = 1, backoff_sec: float = 2.0,
                   sleep=time.sleep, on_error=None):
    """fn() 을 실행하고 실패 시 최대 retries 회 재시도(지수적 backoff).

    Args:
        fn: 인자 없는 호출 가능 객체.
        retries: 추가 재시도 횟수(총 시도 = retries + 1).
        backoff_sec: 재시도 간 대기(초). attempt 배수로 증가.
        sleep: 대기 함수(테스트 주입용).
        on_error: (attempt, exc) 콜백 — 로깅/알림용.

    Returns:
        fn() 의 반환값. 모든 시도 실패 시 마지막 예외를 재-raise.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:      # noqa: BLE001 — 배치 레벨에서 모든 실패를 재시도 대상
            attempt += 1
            if on_error is not None:
                on_error(attempt, e)
            if attempt > retries:
                log.error("재시도 소진(%d회) — 최종 실패: %s", retries, e)
                raise
            wait = backoff_sec * attempt
            log.warning("실패(%d/%d) — %.1f초 후 재시도: %s", attempt, retries, wait, e)
            sleep(wait)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_run_record(job: str, status: str, started_at: str,
                     ended_at: str | None = None, stats: dict | None = None,
                     metrics: dict | None = None, error: str | None = None) -> dict:
    """배치 1회 실행 결과 레코드 생성.

    status: "success" | "failed". elapsed_sec 는 started/ended(ISO) 로 계산.
    """
    ended_at = ended_at or _now_iso()
    try:
        elapsed = (datetime.fromisoformat(ended_at)
                   - datetime.fromisoformat(started_at)).total_seconds()
    except Exception:
        elapsed = None
    return {
        "job": job,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "elapsed_sec": elapsed,
        "stats": stats or {},
        "metrics": metrics or {},
        "error": error,
    }


def append_run_log(path, record: dict) -> None:
    """실행 레코드를 JSONL 1줄로 append(디렉터리 자동 생성)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_run_log(path) -> list[dict]:
    """JSONL 실행 로그를 레코드 리스트로 읽는다(없으면 빈 리스트)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def metrics_delta(prev: dict | None, curr: dict) -> dict:
    """직전 실행 대비 지표 변화량. prev 없으면 변화량 None."""
    delta = {}
    for key, cur_val in curr.items():
        if not isinstance(cur_val, (int, float)):
            continue
        prev_val = (prev or {}).get(key)
        delta[key] = (cur_val - prev_val) if isinstance(prev_val, (int, float)) else None
    return delta


def last_success(records: list[dict], job: str | None = None) -> dict | None:
    """가장 최근 성공 레코드 반환(없으면 None). job 지정 시 해당 job 한정."""
    for rec in reversed(records):
        if rec.get("status") == "success" and (job is None or rec.get("job") == job):
            return rec
    return None
