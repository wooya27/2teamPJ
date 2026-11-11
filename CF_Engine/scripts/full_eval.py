"""
CF_Engine 종합 추천 적절성 평가

모드(--mode):
  fair    (권장) 동일 사용자·동일 정답(홀드아웃) 코호트에서 품질 필터 OFF/ON 비교.
          전체 상호작용에 temporal 분리 1회 → OFF=전체 학습, ON=품질 아이템만 학습.
          정답이 필터로 사라진 유저는 제외하고 그 수를 리포트에 명시.
  legacy  (구방식) 필터 OFF/ON 각각 별도 행렬·별도 평가셋. 집합이 달라 직접 비교 불가.
          비교 기준의 함정을 드러내기 위한 대조용으로만 유지.

지표: NDCG@K, MRR, HitRate@K, Precision@K, Coverage + Cold/Warm/Hot 세그먼트.

실행: python scripts/full_eval.py --mode fair --k 10
      python scripts/full_eval.py --mode legacy --k 10
"""

import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

import yaml
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from src.data_loader import get_conn, load_matrix, load_interactions
from src.als_model import train
from src.evaluation import align_fair_cohort, evaluate_rankings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


def load_quality_item_ids(conn) -> set:
    """poster_url 과 vod_embedding 을 모두 가진 VOD(full_asset_id) 집합."""
    cur = conn.cursor()
    cur.execute("""
        SELECT v.full_asset_id
        FROM public.vod v
        JOIN public.vod_embedding ve ON v.full_asset_id = ve.vod_id_fk
        WHERE v.poster_url IS NOT NULL
    """)
    ids = {r[0] for r in cur.fetchall()}
    cur.close()
    log.info("품질 아이템(poster+embedding): %d개", len(ids))
    return ids


def _recommend_dict(model, train_mat, test_items, k, candidate_items=None):
    users = list(test_items.keys())
    if not users:
        return {}
    kwargs = {}
    if candidate_items is not None:
        # implicit: 추천 후보를 이 아이템 인덱스 부분집합으로 제한
        kwargs["items"] = candidate_items
    item_idx, _ = model.recommend(
        users, train_mat[users], N=k, filter_already_liked_items=True, **kwargs)
    return {u: item_idx[i].tolist() for i, u in enumerate(users)}


def _eval_model(train_mat, test_items, full_mat, m_cfg, k, candidate_items=None):
    model = train(train_mat, factors=m_cfg["factors"],
                  iterations=m_cfg["iterations"],
                  regularization=m_cfg["regularization"])
    rec = _recommend_dict(model, train_mat, test_items, k, candidate_items)
    hist = {u: int(full_mat.getrow(u).nnz) for u in test_items}
    n_items = len(candidate_items) if candidate_items is not None else train_mat.shape[1]
    return evaluate_rankings(rec, test_items, k=k,
                             n_items=n_items, history_by_user=hist)


# ── 모드별 실행 ──────────────────────────────────────────────────────

def run_fair(conn, m_cfg, k):
    """동일 코호트 OFF vs ON 비교."""
    interactions = load_interactions(conn, filter_quality=False)
    quality_items = load_quality_item_ids(conn)
    conn.close()

    aligned = align_fair_cohort(interactions, quality_items, alpha=m_cfg["alpha"])
    test_items = aligned["test_items_fair"]
    full_mat = aligned["full_mat"]

    quality_cols = np.array(aligned["quality_cols"], dtype=np.int64)

    log.info("[OFF] 전체 학습 모델 평가 (후보 전체)")
    res_off = _eval_model(aligned["train_off"], test_items, full_mat, m_cfg, k)
    log.info("[ON] 품질 아이템 학습 모델 평가 (후보를 품질 아이템으로 제한)")
    res_on = _eval_model(aligned["train_on"], test_items, full_mat, m_cfg, k,
                         candidate_items=quality_cols)

    return {
        "mode": "fair",
        "labels": ("필터 OFF (전체 학습)", "필터 ON (품질 아이템 학습)"),
        "results": {"off": res_off, "on": res_on},
        "stats": aligned["stats"],
    }


def run_legacy(conn, m_cfg, k):
    """구방식: 필터 OFF/ON 각각 별도 행렬·별도 평가셋(집합 불일치).

    행렬만 있어 시간순 분리가 불가하므로 재현 가능한 무작위 분리(seed=42)를 쓴다.
    """
    from src.evaluation import random_holdout_split

    out = {}
    for idx, (key, use_filter) in enumerate([("off", False), ("on", True)]):
        c = conn if idx == 0 else get_conn()   # 두 번째 반복은 새 커넥션
        mat, *_ = load_matrix(c, alpha=m_cfg["alpha"], filter_quality=use_filter)
        c.close()
        train_mat, test_items = random_holdout_split(mat, seed=42)
        out[key] = _eval_model(train_mat, test_items, mat, m_cfg, k)
    return {
        "mode": "legacy",
        "labels": ("필터 OFF (별도 집합)", "필터 ON (별도 집합)"),
        "results": out,
        "stats": {"note": "OFF/ON 평가 사용자·아이템 집합이 서로 다름 — 직접 비교 불가"},
    }


# ── 리포트 ───────────────────────────────────────────────────────────

def write_report(payload, m_cfg, k):
    labels = payload["labels"]
    off = payload["results"]["off"]["overall"]
    on = payload["results"]["on"]["overall"]
    seg_off = payload["results"]["off"]["segments"]
    seg_on = payload["results"]["on"]["segments"]
    stats = payload["stats"]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path("docs") / f"full_eval_report_{ts}.md"
    path.parent.mkdir(exist_ok=True)

    L = []
    L.append(f"# CF_Engine 종합 평가 리포트 ({payload['mode']})\n\n")
    L.append(f"- 일시: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    L.append(f"- 모델: ALS factors={m_cfg['factors']}, iterations={m_cfg['iterations']}, "
             f"regularization={m_cfg['regularization']}\n")
    L.append(f"- k={k}\n\n")

    if payload["mode"] == "fair":
        L.append("## 공정 비교 조건\n\n")
        L.append("전체 상호작용에 시간순(temporal) 분리를 1회 적용해 **동일 사용자·동일 "
                 "정답**을 고정한 뒤, 품질 필터만 바꿔 평가했다.\n\n")
        L.append(f"- 전체 평가 가능 유저: {stats['eval_users_all']:,}명\n")
        L.append(f"- **공정 평가 유저(정답이 품질 아이템): {stats['eval_users_fair']:,}명**\n")
        L.append(f"- 정답이 품질 필터로 제외되어 빠진 유저: "
                 f"{stats['excluded_holdout_filtered']:,}명\n")
        L.append(f"- 전체 아이템 {stats['n_items_total']:,}개 중 품질 아이템 "
                 f"{stats['n_items_quality']:,}개\n\n")
    else:
        L.append("## ⚠ 구방식 경고\n\n")
        L.append(f"> {stats.get('note')}\n\n")

    metric_keys = [f"NDCG@{k}", "MRR", f"HitRate@{k}", f"Precision@{k}", "Coverage"]
    L.append("## 전체 성능 비교\n\n")
    L.append(f"| 지표 | {labels[0]} | {labels[1]} | 차이 |\n|---|---|---|---|\n")
    for key in metric_keys:
        v0 = off.get(key, 0.0)
        v1 = on.get(key, 0.0)
        d = v1 - v0
        L.append(f"| {key} | {v0:.4f} | {v1:.4f} | {d:+.4f} {'↑' if d > 0 else '↓'} |\n")
    L.append(f"\n> 평가 유저: {off['eval_users']:,}명 (양쪽 동일)\n\n"
             if payload["mode"] == "fair"
             else f"\n> OFF {off['eval_users']:,}명 / ON {on['eval_users']:,}명 (서로 다름)\n\n")

    L.append("## 세그먼트별 (유저 이력 수)\n\n")
    for label, seg in [(labels[0], seg_off), (labels[1], seg_on)]:
        L.append(f"### {label}\n\n")
        L.append(f"| 세그먼트 | 유저 수 | NDCG | MRR | HitRate |\n|---|---|---|---|---|\n")
        for name, s in seg.items():
            L.append(f"| {name} | {s['users']:,} | {s['NDCG']:.4f} | {s['MRR']:.4f} | {s['HitRate']:.4f} |\n")
        L.append("\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(L)
    log.info("리포트 저장: %s", path.resolve())
    print(f"\n▶ 리포트: {path.resolve()}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/als_config.yaml")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--mode", choices=["fair", "legacy"], default="fair")
    args = parser.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    m_cfg = cfg["model"]

    t0 = time.time()
    conn = get_conn()
    payload = run_fair(conn, m_cfg, args.k) if args.mode == "fair" \
        else run_legacy(conn, m_cfg, args.k)
    write_report(payload, m_cfg, args.k)
    log.info("소요: %.1f초", time.time() - t0)


if __name__ == "__main__":
    main()
