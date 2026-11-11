"""추천 결과 → serving.vod_recommendation 적재(라이브러리).

재실행 안전성(idempotency)·장애 복구를 위해:
  - DELETE + INSERT 를 **단일 트랜잭션**으로 처리하고, 실패 시 **rollback 후 재-raise**.
  - 동일 배치를 다시 실행해도 결과가 중복되지 않는다(해당 type·user 삭제 후 재삽입).
  - 건수·소요 시간·에러를 로그에 남기고 stats 로 반환.

두 가지 적재 방식 제공:
  export()        DELETE + INSERT (기본). 단순·확실하게 stale 제거.
  export_upsert() ON CONFLICT DO UPDATE + stale prune. 변경 없는 행은 제자리 UPDATE.

scripts/export_to_db.py 가 이 모듈을 재노출한다(train.py 하위호환).
"""

from __future__ import annotations

import time
import logging

from psycopg2.extras import execute_batch   # 테스트에서 monkeypatch 가능하도록 모듈 전역

log = logging.getLogger(__name__)

INSERT_SQL = """
    INSERT INTO serving.vod_recommendation
        (user_id_fk, vod_id_fk, rank, score, recommendation_type)
    VALUES
        (%(user_id_fk)s, %(vod_id_fk)s, %(rank)s, %(score)s, %(recommendation_type)s)
"""

UPSERT_SQL = """
    INSERT INTO serving.vod_recommendation
        (user_id_fk, vod_id_fk, rank, score, recommendation_type)
    VALUES
        (%(user_id_fk)s, %(vod_id_fk)s, %(rank)s, %(score)s, %(recommendation_type)s)
    ON CONFLICT (user_id_fk, vod_id_fk, recommendation_type)
    DO UPDATE SET rank = EXCLUDED.rank, score = EXCLUDED.score
"""

DELETE_SQL = """
    DELETE FROM serving.vod_recommendation
    WHERE recommendation_type = %s
      AND user_id_fk = ANY(%s)
"""

# UPSERT 모드에서 신규 집합에 없는 기존(stale) 추천 제거
DELETE_STALE_SQL = """
    DELETE FROM serving.vod_recommendation
    WHERE recommendation_type = %s
      AND user_id_fk = ANY(%s)
      AND NOT (vod_id_fk = ANY(%s))
"""


def _clip_scores(records: list[dict]) -> None:
    """ALS score 가 부동소수 오차로 1.0 을 넘으면 DB CHECK(score<=1) 위반 → 클리핑."""
    for r in records:
        r["score"] = min(float(r["score"]), 1.0)


def export(conn, records: list[dict], batch_size: int = 1000,
           recommendation_type: str = "COLLABORATIVE") -> dict:
    """DELETE + INSERT 단일 트랜잭션 적재. 실패 시 rollback 후 예외 재-raise.

    Returns:
        stats: {"deleted", "inserted", "users", "elapsed_sec", "method"}.
    """
    if not records:
        log.warning("저장할 레코드 없음 — 적재 건너뜀")
        return {"deleted": 0, "inserted": 0, "users": 0, "elapsed_sec": 0.0,
                "method": "delete_insert"}

    user_ids = list({r["user_id_fk"] for r in records})
    _clip_scores(records)
    cur = conn.cursor()
    t0 = time.time()
    try:
        log.info("기존 CF 추천 삭제 중 (유저 %d명)...", len(user_ids))
        cur.execute(DELETE_SQL, (recommendation_type, user_ids))
        deleted = cur.rowcount
        log.info("삭제 완료: %d건", deleted)

        log.info("신규 추천 INSERT 중 (%d건, 배치 %d)...", len(records), batch_size)
        total = 0
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            execute_batch(cur, INSERT_SQL, batch, page_size=batch_size)
            total += len(batch)
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("적재 실패 — 롤백 완료 (삽입 0건 유지): %s", e)
        raise
    finally:
        cur.close()

    elapsed = time.time() - t0
    log.info("DB 적재 완료: %d건 / 유저 %d명 / %.1f초", total, len(user_ids), elapsed)
    return {"deleted": deleted, "inserted": total, "users": len(user_ids),
            "elapsed_sec": elapsed, "method": "delete_insert"}


def export_upsert(conn, records: list[dict], batch_size: int = 1000,
                  recommendation_type: str = "COLLABORATIVE",
                  prune_stale: bool = True) -> dict:
    """ON CONFLICT DO UPDATE 적재(+ stale prune). 실패 시 rollback 후 재-raise.

    변경 없는 추천은 삭제·재삽입 대신 제자리 UPDATE 되므로, 조회 측에서 추천이
    일시적으로 비는 구간(DELETE→INSERT 사이)이 없다. 대신 신규 집합에 없는 기존
    추천(stale)은 별도 DELETE 로 정리해야 한다(prune_stale=True).
    """
    if not records:
        log.warning("저장할 레코드 없음 — 적재 건너뜀")
        return {"upserted": 0, "pruned": 0, "users": 0, "elapsed_sec": 0.0,
                "method": "upsert"}

    user_ids = list({r["user_id_fk"] for r in records})
    new_vods = list({r["vod_id_fk"] for r in records})
    _clip_scores(records)
    cur = conn.cursor()
    t0 = time.time()
    try:
        total = 0
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            execute_batch(cur, UPSERT_SQL, batch, page_size=batch_size)
            total += len(batch)
        pruned = 0
        if prune_stale:
            cur.execute(DELETE_STALE_SQL, (recommendation_type, user_ids, new_vods))
            pruned = cur.rowcount
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error("UPSERT 적재 실패 — 롤백 완료: %s", e)
        raise
    finally:
        cur.close()

    elapsed = time.time() - t0
    log.info("UPSERT 적재 완료: %d건 upsert / %d건 prune / %.1f초", total, pruned, elapsed)
    return {"upserted": total, "pruned": pruned, "users": len(user_ids),
            "elapsed_sec": elapsed, "method": "upsert"}
