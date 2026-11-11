"""
추천 품질 평가 (Hold-out 방식)

분리 방식(--split):
  temporal  시청 시각(strt_dt) 기준 유저별 최근 1건 분리 (기본, 권장)
  random    고정 시드 무작위 분리 (시각 미사용 대안)
  legacy    기존 nz[-1] 방식 — 재현/비교 목적으로만 유지(정확하지 않음)

실행: python scripts/evaluate.py
      python scripts/evaluate.py --split temporal --k 20
      python scripts/evaluate.py --split random --seed 42 --filter-quality
"""

import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime

import yaml
from scipy.sparse import csr_matrix

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.data_loader import get_conn, load_matrix, load_interactions
from src.als_model import train
from src.evaluation import (
    temporal_holdout_split,
    random_holdout_split,
    evaluate_rankings,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def legacy_split_holdout(mat: csr_matrix):
    """[DEPRECATED] 기존 nz[-1] 방식. 마지막 '시청 시각'이 아니라 최대 '컬럼 인덱스'를
    고르던 버그를 재현/비교하기 위해서만 남겨 둔다. 신규 평가는 temporal/random 사용."""
    train_data = mat.copy().tolil()
    test_items = {}
    for uid in range(mat.shape[0]):
        nz = mat.getrow(uid).nonzero()[1]
        if len(nz) < 2:
            continue
        hold = int(nz[-1])
        test_items[uid] = hold
        train_data[uid, hold] = 0
    return csr_matrix(train_data), test_items


def _recommend_to_dict(model, train_mat, test_items, k):
    """implicit 모델 추천 결과를 user_idx -> [item_idx,...] 딕셔너리로 변환."""
    user_list = list(test_items.keys())
    item_indices, _ = model.recommend(
        user_list, train_mat[user_list], N=k, filter_already_liked_items=True,
    )
    return {uid: item_indices[i].tolist() for i, uid in enumerate(user_list)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/als_config.yaml")
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument("--split", choices=["temporal", "random", "legacy"],
                        default="temporal", help="Hold-out 분리 방식")
    parser.add_argument("--seed", type=int, default=42, help="random 분리 시드")
    parser.add_argument("--filter-quality", action="store_true",
                        help="poster_url + vod_embedding 둘 다 있는 VOD만 사용")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    m = cfg["model"]

    t0 = time.time()
    conn = get_conn()

    if args.split == "temporal":
        interactions = load_interactions(conn, filter_quality=args.filter_quality)
        conn.close()
        log.info("Hold-out 분리(temporal) 중...")
        train_mat, test_items, full_mat, (u_enc, i_enc, u_dec, i_dec) = \
            temporal_holdout_split(interactions, alpha=m["alpha"])
    else:
        mat, u_enc, i_enc, u_dec, i_dec = load_matrix(
            conn, alpha=m["alpha"], filter_quality=args.filter_quality)
        conn.close()
        log.info("Hold-out 분리(%s) 중...", args.split)
        if args.split == "random":
            train_mat, test_items = random_holdout_split(mat, seed=args.seed)
        else:
            train_mat, test_items = legacy_split_holdout(mat)
        full_mat = mat

    log.info("테스트 유저: %d명", len(test_items))

    model = train(train_mat, factors=m["factors"], iterations=m["iterations"],
                  regularization=m["regularization"])

    log.info("평가 중 (k=%d)...", args.k)
    rec_by_user = _recommend_to_dict(model, train_mat, test_items, args.k)
    history_by_user = {u: int(full_mat.getrow(u).nnz) for u in test_items}
    result = evaluate_rankings(rec_by_user, test_items, k=args.k,
                               n_items=train_mat.shape[1],
                               history_by_user=history_by_user)
    metrics = result["overall"]

    log.info("=" * 45)
    for key, val in metrics.items():
        if isinstance(val, float):
            log.info("  %-15s %.4f", key, val)
        else:
            log.info("  %-15s %d", key, val)
    elapsed = time.time() - t0
    log.info("  소요: %.1f초", elapsed)
    log.info("=" * 45)

    # 리포트 저장
    report_dir = Path("docs")
    report_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"eval_report_{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# CF_Engine 평가 리포트 — {date_str}\n\n")
        f.write(f"| 지표 | 값 |\n|------|----|\n")
        for key, val in metrics.items():
            f.write(f"| {key} | {f'{val:.4f}' if isinstance(val, float) else val} |\n")
        f.write(f"\n- 모델: ALS factors={m['factors']}, iterations={m['iterations']}\n")
        f.write(f"- k={args.k}\n")
        f.write(f"- 분리 방식: {args.split}" + (f" (seed={args.seed})" if args.split == "random" else "") + "\n")
        f.write(f"- 품질 필터: {'ON (poster_url + vod_embedding 있는 VOD만)' if args.filter_quality else 'OFF (전체)'}\n")
        f.write(f"- 소요: {elapsed:.1f}초\n\n")
        f.write("## 세그먼트별 (유저 이력 수)\n\n")
        f.write(f"| 세그먼트 | 유저 수 | NDCG | MRR | HitRate |\n|---|---|---|---|---|\n")
        for name, s in result["segments"].items():
            f.write(f"| {name} | {s['users']} | {s['NDCG']:.4f} | {s['MRR']:.4f} | {s['HitRate']:.4f} |\n")
    log.info("리포트 저장: %s", report_path)


if __name__ == "__main__":
    main()
