"""
CF_Engine 정기 배치 실행 래퍼 (자동화·모니터링)

train 파이프라인을 재시도·구조화 로그와 함께 실행한다.
- 성공/실패, 처리 건수, 소요 시간, 지표 변화를 logs/run_log.jsonl 에 1줄씩 기록.
- 실패 시 안전 재시도(backoff) 후에도 실패하면 알림 훅 호출 + 비정상 종료코드.
- cron / Cloud Run Jobs / 작업 스케줄러에서 이 스크립트를 호출한다.

실행:
  python scripts/run_pipeline.py                 # 학습+적재, 기본 재시도
  python scripts/run_pipeline.py --dry-run       # DB 저장 없이
  python scripts/run_pipeline.py --retries 2     # 추가 재시도 2회
"""

import os
import sys
import logging
import argparse
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from src.data_loader import get_conn
from src.db_writer import export
from src.runconfig import resolve_filter_quality, describe_run
from src.monitoring import (
    run_with_retry, build_run_record, append_run_log, read_run_log,
    metrics_delta, last_success, _now_iso,
)
from train import load_config, run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

LOG_PATH = ROOT / "logs" / "run_log.jsonl"


def send_alert(message: str) -> None:
    """실패 알림 훅. CF_ALERT_WEBHOOK 환경변수가 있으면 전송(없으면 로그만).

    외부 URL·자격증명은 환경변수로만 받는다(하드코딩 금지 — 루트 CLAUDE.md 보안규칙).
    """
    log.error("[ALERT] %s", message)
    webhook = os.getenv("CF_ALERT_WEBHOOK")
    if not webhook:
        return
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": message}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:      # 알림 실패가 배치 실패를 가리지 않도록 삼킴
        log.warning("알림 전송 실패: %s", e)


def main():
    parser = argparse.ArgumentParser(description="CF_Engine 정기 배치 래퍼")
    parser.add_argument("--config", default="config/als_config.yaml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--filter-quality", action="store_true")
    parser.add_argument("--no-filter-quality", action="store_true")
    parser.add_argument("--retries", type=int, default=1, help="추가 재시도 횟수")
    parser.add_argument("--backoff", type=float, default=5.0, help="재시도 대기(초)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    filter_quality = resolve_filter_quality(cfg, args.filter_quality, args.no_filter_quality)
    settings = describe_run(cfg, filter_quality, args.dry_run)
    started = _now_iso()

    def _job():
        records, shape = run_pipeline(cfg, filter_quality=filter_quality)
        inserted = 0
        if not args.dry_run:
            conn = get_conn()
            stats = export(conn, records,
                           batch_size=cfg["db"]["batch_size"],
                           recommendation_type=cfg["recommend"]["recommendation_type"])
            conn.close()
            inserted = stats.get("inserted", 0)
        return {"users": shape[0], "items": shape[1],
                "records": len(records), "inserted": inserted}

    prev = last_success(read_run_log(LOG_PATH), job="cf_train")

    try:
        job_stats = run_with_retry(
            _job, retries=args.retries, backoff_sec=args.backoff,
            on_error=lambda a, e: log.warning("시도 %d 실패: %s", a, e))
        record = build_run_record(
            "cf_train", "success", started, _now_iso(),
            stats={**settings, **job_stats},
            metrics=metrics_delta(prev.get("stats") if prev else None, job_stats))
        append_run_log(LOG_PATH, record)
        log.info("배치 성공 — 기록: %s", LOG_PATH)
        log.info("처리: %s", job_stats)
    except Exception as e:
        record = build_run_record(
            "cf_train", "failed", started, _now_iso(),
            stats=settings, error=str(e))
        append_run_log(LOG_PATH, record)
        send_alert(f"CF_Engine 배치 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
