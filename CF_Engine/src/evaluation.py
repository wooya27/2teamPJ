"""CF_Engine 평가 유틸 — Hold-out 분리 및 랭킹 지표.

순수 함수 모듈. DB(psycopg2)·implicit 의존이 없어 단위 테스트에서 그대로 호출 가능하다.
scripts/evaluate.py, scripts/full_eval.py 가 분리/지표 계산을 이 모듈에 위임한다.

분리 방식
---------
1) temporal (권장): watch_history.strt_dt 기준 유저별 '가장 최근 시청' 아이템 1건을
   테스트 정답으로 분리한다(leave-latest-out). 동일 (유저, VOD) 반복 시청은
   최신 시각·최대 confidence로 집계하므로 홀드아웃 아이템이 학습셋에 남지 않는다.
2) random: strt_dt 를 쓸 수 없을 때를 위한 재현 가능한 대안. 고정 시드로 유저별
   비영(非零) 아이템 1건을 무작위 분리한다. 한계(시간 누수 가능)를 리포트에 명시한다.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy.sparse import csr_matrix

log = logging.getLogger(__name__)


# ── 인코더 / 행렬 구성 ────────────────────────────────────────────────

def build_encoders(interactions):
    """interactions: iterable of (user_id, item_id, ...). 정렬 기반 결정적 인코더 생성."""
    user_ids = sorted({r[0] for r in interactions})
    item_ids = sorted({r[1] for r in interactions})
    user_encoder = {u: i for i, u in enumerate(user_ids)}
    item_encoder = {v: i for i, v in enumerate(item_ids)}
    user_decoder = {i: u for u, i in user_encoder.items()}
    item_decoder = {i: v for v, i in item_encoder.items()}
    return user_encoder, item_encoder, user_decoder, item_decoder


def _confidence(completion_rate, alpha: float) -> float:
    cr = 0.0 if completion_rate is None else float(completion_rate)
    return 1.0 + alpha * cr


# ── 시간순(leave-latest-out) 분리 ────────────────────────────────────

def temporal_holdout_split(interactions, alpha: float = 40.0, min_history: int = 2):
    """시청 시각(strt_dt) 기준 유저별 최근 1건을 테스트로 분리.

    Args:
        interactions: (user_id, item_id, completion_rate, strt_dt) 4-튜플의 iterable.
            strt_dt 는 정렬 비교만 가능하면 된다(datetime 또는 ISO 문자열).
        alpha: confidence = 1 + alpha * completion_rate.
        min_history: 서로 다른 시청 아이템 수가 이 값 미만인 유저는 제외(평가 불가).

    Returns:
        train_mat (csr_matrix): 홀드아웃을 제거한 학습 행렬.
        test_items (dict[int, int]): user_idx -> 정답 item_idx.
        full_mat  (csr_matrix): 홀드아웃 포함 전체 행렬(세그먼트 이력 수 계산용).
        encoders  (tuple): (user_encoder, item_encoder, user_decoder, item_decoder).

    집계 규칙:
        동일 (유저, VOD) 반복 시청 → 최신 strt_dt, 최대 confidence 로 1개 셀에 집계.
        같은 유저 내 최신 시각이 동률이면 item_idx 가 큰 쪽을 홀드아웃으로 선택(결정적).
    """
    interactions = list(interactions)
    user_encoder, item_encoder, user_decoder, item_decoder = build_encoders(interactions)

    # (uidx, iidx) -> {"ts": 최신시각, "conf": 최대confidence}
    cell: dict[tuple[int, int], dict] = {}
    for user_id, item_id, completion_rate, strt_dt in interactions:
        u = user_encoder[user_id]
        i = item_encoder[item_id]
        conf = _confidence(completion_rate, alpha)
        prev = cell.get((u, i))
        if prev is None:
            cell[(u, i)] = {"ts": strt_dt, "conf": conf}
        else:
            if strt_dt > prev["ts"]:
                prev["ts"] = strt_dt
            if conf > prev["conf"]:
                prev["conf"] = conf

    # 유저별 아이템 목록
    by_user: dict[int, list[tuple[int, object, float]]] = {}
    for (u, i), v in cell.items():
        by_user.setdefault(u, []).append((i, v["ts"], v["conf"]))

    test_items: dict[int, int] = {}
    train_rows, train_cols, train_conf = [], [], []
    full_rows, full_cols, full_conf = [], [], []

    for u, items in by_user.items():
        for i, ts, conf in items:
            full_rows.append(u)
            full_cols.append(i)
            full_conf.append(conf)

        if len(items) < min_history:
            # 평가 불가 유저: 전부 학습셋으로 유지, 테스트 대상에서 제외
            for i, ts, conf in items:
                train_rows.append(u)
                train_cols.append(i)
                train_conf.append(conf)
            continue

        # 최신 시각(동률 시 item_idx 큰 쪽)을 홀드아웃으로 선택
        hold_i, _, _ = max(items, key=lambda t: (t[1], t[0]))
        test_items[u] = hold_i
        for i, ts, conf in items:
            if i == hold_i:
                continue
            train_rows.append(u)
            train_cols.append(i)
            train_conf.append(conf)

    shape = (len(user_encoder), len(item_encoder))
    train_mat = csr_matrix(
        (train_conf, (train_rows, train_cols)), shape=shape, dtype=np.float32
    )
    full_mat = csr_matrix(
        (full_conf, (full_rows, full_cols)), shape=shape, dtype=np.float32
    )
    log.info(
        "temporal 분리: 유저 %d명 중 평가 대상 %d명 (min_history=%d)",
        shape[0], len(test_items), min_history,
    )
    return train_mat, test_items, full_mat, (
        user_encoder, item_encoder, user_decoder, item_decoder,
    )


# ── 재현 가능한 무작위 분리(대안) ────────────────────────────────────

def random_holdout_split(mat: csr_matrix, seed: int = 42, min_history: int = 2):
    """고정 시드로 유저별 비영 아이템 1건을 무작위 분리(시각 컬럼이 없을 때의 대안).

    Returns:
        (train_mat, test_items).  test_items: user_idx -> item_idx.
    주의: 시간 정보를 쓰지 않으므로 미래→과거 정보 누수가 발생할 수 있다(리포트에 명시).
    """
    rng = np.random.default_rng(seed)
    train = mat.tolil(copy=True)
    test_items: dict[int, int] = {}
    for u in range(mat.shape[0]):
        nz = mat.getrow(u).nonzero()[1]
        if len(nz) < min_history:
            continue
        hold = int(rng.choice(nz))
        test_items[u] = hold
        train[u, hold] = 0
    return train.tocsr(), test_items


# ── 공정 비교: 동일 코호트 정렬(P0-3) ───────────────────────────────

def restrict_matrix_columns(mat: csr_matrix, keep_cols) -> csr_matrix:
    """keep_cols(아이템 인덱스 집합)에 없는 컬럼의 값을 0으로 만든 행렬 반환.

    품질 필터 ON 모델이 '품질 아이템만' 학습·추천하도록 후보 풀을 제한한다.
    행렬의 shape(인코더 공간)는 유지하므로 OFF/ON 모델의 인덱스가 호환된다.
    """
    keep = set(int(c) for c in keep_cols)
    coo = mat.tocoo()
    rows, cols, data = [], [], []
    for r, c, v in zip(coo.row, coo.col, coo.data):
        if int(c) in keep:
            rows.append(r)
            cols.append(c)
            data.append(v)
    return csr_matrix((data, (rows, cols)), shape=mat.shape, dtype=np.float32)


def align_fair_cohort(interactions, quality_items, alpha: float = 40.0,
                      min_history: int = 2):
    """품질 필터 ON/OFF 를 '동일 사용자·동일 정답' 으로 비교하기 위한 정렬.

    절차:
      1) 전체 상호작용에 temporal 분리를 1회 적용 → 유저별 고정 홀드아웃·학습 이력 확정.
      2) OFF 학습행렬 = 전체 train_mat.
         ON  학습행렬 = train_mat 에서 품질 아이템 컬럼만 유지(restrict_matrix_columns).
      3) 공정 평가 유저 = 홀드아웃 정답이 '품질 아이템' 인 유저(두 모델 모두 맞힐 수
         있는 집합). 품질 필터로 정답이 사라지는 유저는 제외하고 그 수를 보고.

    Returns:
        dict {
          "train_off", "train_on": csr_matrix (동일 shape),
          "test_items_fair": {user_idx: item_idx},   # 정답이 품질 아이템인 유저만
          "encoders": (u_enc, i_enc, u_dec, i_dec),
          "full_mat": csr_matrix,                      # 세그먼트 이력 수 계산용
          "stats": {"eval_users_all", "eval_users_fair", "excluded_holdout_filtered",
                    "n_items_total", "n_items_quality"},
        }
    """
    train_off, test_items, full_mat, encoders = temporal_holdout_split(
        interactions, alpha=alpha, min_history=min_history)
    u_enc, i_enc, u_dec, i_dec = encoders

    quality_cols = {i_enc[v] for v in quality_items if v in i_enc}
    train_on = restrict_matrix_columns(train_off, quality_cols)

    test_items_fair = {
        u: it for u, it in test_items.items() if it in quality_cols
    }
    stats = {
        "eval_users_all": len(test_items),
        "eval_users_fair": len(test_items_fair),
        "excluded_holdout_filtered": len(test_items) - len(test_items_fair),
        "n_items_total": train_off.shape[1],
        "n_items_quality": len(quality_cols),
    }
    log.info(
        "공정 코호트: 전체 평가 %d명 → 공정 평가 %d명 (정답이 필터로 제외된 유저 %d명)",
        stats["eval_users_all"], stats["eval_users_fair"],
        stats["excluded_holdout_filtered"],
    )
    return {
        "train_off": train_off,
        "train_on": train_on,
        "test_items_fair": test_items_fair,
        "encoders": encoders,
        "full_mat": full_mat,
        "stats": stats,
    }


# ── 랭킹 지표(순수 함수) ─────────────────────────────────────────────

def ndcg_at_k(recommended, relevant: int, k: int) -> float:
    """단일 정답(binary relevance) NDCG@k. idcg = 1 / log2(2) = 1.0."""
    dcg = 0.0
    for idx, item in enumerate(recommended[:k]):
        if item == relevant:
            dcg += 1.0 / np.log2(idx + 2)
            break
    return dcg  # idcg == 1.0


def reciprocal_rank(recommended, relevant: int, k: int) -> float:
    for idx, item in enumerate(recommended[:k]):
        if item == relevant:
            return 1.0 / (idx + 1)
    return 0.0


def hit(recommended, relevant: int, k: int) -> int:
    return int(relevant in list(recommended)[:k])


def evaluate_rankings(rec_by_user: dict, relevant_by_user: dict, k: int,
                      n_items: int | None = None,
                      history_by_user: dict | None = None) -> dict:
    """추천 결과 딕셔너리로부터 전체/세그먼트 지표 계산(implicit 불필요).

    Args:
        rec_by_user: user_idx -> 추천 item_idx 리스트(랭크 순).
        relevant_by_user: user_idx -> 정답 item_idx.
        k: cut-off.
        n_items: Coverage 분모(전체 아이템 수). None이면 Coverage 생략.
        history_by_user: user_idx -> 학습 이력 개수. 주어지면 Cold/Warm/Hot 세그먼트 집계.

    Returns:
        {"overall": {...}, "segments": {...}} 형태의 지표 딕셔너리.
    """
    ndcgs, mrrs, hits_, precisions = [], [], [], []
    covered: set[int] = set()
    seg = {"cold (2~4)": [], "warm (5~19)": [], "hot (20+)": []}

    users = list(relevant_by_user.keys())
    for u in users:
        relevant = relevant_by_user[u]
        recs = list(rec_by_user.get(u, []))
        covered.update(recs[:k])

        h = hit(recs, relevant, k)
        nd = ndcg_at_k(recs, relevant, k)
        rr = reciprocal_rank(recs, relevant, k)
        hits_.append(h)
        ndcgs.append(nd)
        mrrs.append(rr)
        precisions.append(h / k)

        if history_by_user is not None:
            hc = history_by_user.get(u, 0)
            bucket = "cold (2~4)" if hc < 5 else ("warm (5~19)" if hc < 20 else "hot (20+)")
            seg[bucket].append((nd, rr, h))

    overall = {
        f"NDCG@{k}": float(np.mean(ndcgs)) if ndcgs else 0.0,
        "MRR": float(np.mean(mrrs)) if mrrs else 0.0,
        f"HitRate@{k}": float(np.mean(hits_)) if hits_ else 0.0,
        f"Precision@{k}": float(np.mean(precisions)) if precisions else 0.0,
        "eval_users": len(users),
    }
    if n_items:
        overall["Coverage"] = len(covered) / n_items

    segments = {}
    for name, data in seg.items():
        if not data:
            segments[name] = {"users": 0, "NDCG": 0.0, "MRR": 0.0, "HitRate": 0.0}
            continue
        arr = np.array(data, dtype=float)
        segments[name] = {
            "users": len(data),
            "NDCG": float(arr[:, 0].mean()),
            "MRR": float(arr[:, 1].mean()),
            "HitRate": float(arr[:, 2].mean()),
        }
    return {"overall": overall, "segments": segments}
