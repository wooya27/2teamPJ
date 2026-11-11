"""
CF_Engine 전체 파이프라인 실행

실행:
  python scripts/train.py                # DB 학습 + 적재
  python scripts/train.py --dry-run      # DB 저장 없이 추천만 생성
"""

import sys
import time
import logging
import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")

from src.data_loader import get_conn, load_matrix
from src.als_model import train, recommend_all
from src.recommender import build_records, load_vod_series_map, load_popular_items, apply_fallback
from src.runconfig import resolve_filter_quality, describe_run
from export_to_db import export

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_pipeline(cfg: dict, filter_quality: bool = False) -> tuple:
    """DB 로드 → ALS 학습 → 추천 생성 → 시리즈 중복 제거 → 레코드 변환. (mat shape 반환)

    filter_quality 를 load_matrix 에 전달해 학습과 평가가 동일한 필터 정의를 쓰도록 한다.
    """
    m = cfg["model"]
    r = cfg["recommend"]
    top_k = r["top_k"]

    conn = get_conn()
    log.info("DB 접속 완료")
    mat, user_enc, item_enc, user_dec, item_dec = load_matrix(
        conn, alpha=m["alpha"], filter_quality=filter_quality)
    log.info("학습 행렬: 유저 %d명 | 아이템 %d개 | 상호작용 %d건 | 품질 필터 %s",
             mat.shape[0], mat.shape[1], mat.nnz, "ON" if filter_quality else "OFF")
    vod_series_map = load_vod_series_map(conn)
    popular_items = (load_popular_items(conn, limit=r.get("fallback_limit", 200))
                     if r.get("fallback") else [])
    conn.close()

    model = train(mat, factors=m["factors"], iterations=m["iterations"],
                  regularization=m["regularization"])
    user_ids, item_indices, scores = recommend_all(model, mat, top_k=top_k)
    records = build_records(user_ids, item_indices, scores,
                            user_dec, item_dec,
                            recommendation_type=r["recommendation_type"],
                            top_k=top_k,
                            vod_series_map=vod_series_map)

    if r.get("fallback") and popular_items:
        all_user_ids = [user_dec[u] for u in user_ids]
        watched_by_user = _watched_for_underfilled(
            records, all_user_ids, mat, item_dec, user_enc, top_k)
        records, fb_stats = apply_fallback(
            records, popular_items, top_k=top_k,
            recommendation_type=r["recommendation_type"],
            vod_series_map=vod_series_map,
            watched_by_user=watched_by_user,
            all_user_ids=all_user_ids)
        log.info("Fallback 통계: %s", fb_stats)

    return records, mat.shape


def _watched_for_underfilled(records, all_user_ids, mat, item_dec, user_enc, top_k):
    """top_k 미만(0건 포함) 유저에 한해 기시청 VOD 집합을 구성(메모리 절약).

    인기 Fallback 후보에서 이미 시청한 VOD를 제외하기 위해 필요하다. 전체 유저가
    아니라 보충이 필요한 유저만 계산한다.
    """
    cnt: dict = {}
    for rec in records:
        cnt[rec["user_id_fk"]] = cnt.get(rec["user_id_fk"], 0) + 1
    underfilled = [uid for uid in all_user_ids if cnt.get(uid, 0) < top_k]

    watched: dict = {}
    for uid in underfilled:
        u = user_enc.get(uid)
        if u is None:
            continue
        cols = mat.getrow(u).nonzero()[1]
        watched[uid] = {item_dec[c] for c in cols}
    return watched


def main():
    parser = argparse.ArgumentParser(description="CF_Engine 파이프라인")
    parser.add_argument("--config", default="config/als_config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB 저장 없이 추천 결과만 확인")
    parser.add_argument("--filter-quality", action="store_true",
                        help="poster_url + vod_embedding 둘 다 있는 VOD만 사용 (config override)")
    parser.add_argument("--no-filter-quality", action="store_true",
                        help="품질 필터 강제 OFF (config override)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    d = cfg["db"]
    r = cfg["recommend"]

    filter_quality = resolve_filter_quality(cfg, args.filter_quality, args.no_filter_quality)
    settings = describe_run(cfg, filter_quality, args.dry_run)

    t0 = time.time()
    log.info("=" * 55)
    log.info("CF_Engine 학습 시작")
    log.info("확정 설정: %s", settings)   # --dry-run 에서도 실제 선택된 설정 확인 가능
    log.info("=" * 55)
    records, shape = run_pipeline(cfg, filter_quality=filter_quality)

    elapsed = time.time() - t0
    log.info("=" * 55)
    log.info("유저: %d명  |  아이템: %d개  |  추천: %d건  |  소요: %.1f초",
             shape[0], shape[1], len(records), elapsed)

    if args.dry_run:
        log.info("dry-run 모드 — DB 저장 생략")
    else:
        conn = get_conn()
        export(conn, records, batch_size=d["batch_size"],
               recommendation_type=r["recommendation_type"])
        conn.close()

    log.info("완료")


if __name__ == "__main__":
    main()
