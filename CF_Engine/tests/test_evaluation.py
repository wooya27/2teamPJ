"""tests/test_evaluation.py — Hold-out 분리 및 랭킹 지표 단위 테스트.

implicit·DB 없이 순수 numpy/scipy 로 실행된다.
"""

import sys
from datetime import datetime, timedelta

import numpy as np
import pytest
from scipy.sparse import csr_matrix

sys.path.insert(0, ".")

from CF_Engine.src.evaluation import (
    build_encoders,
    temporal_holdout_split,
    random_holdout_split,
    ndcg_at_k,
    reciprocal_rank,
    hit,
    evaluate_rankings,
)


def _ts(day):
    return datetime(2026, 1, 1) + timedelta(days=day)


# ── temporal 분리 ────────────────────────────────────────────────────

def test_temporal_holds_out_latest_by_time():
    """가장 '최근 시각' 아이템이 홀드아웃이어야 한다 (컬럼 인덱스 최댓값이 아니라)."""
    # user1: vodA(day2)가 가장 최근이지만 정렬상 vodC(day1)의 인덱스가 더 큼
    interactions = [
        ("user1", "vodC", 0.5, _ts(1)),
        ("user1", "vodB", 0.5, _ts(0)),
        ("user1", "vodA", 0.9, _ts(2)),  # 최신
    ]
    train_mat, test_items, full_mat, (u_enc, i_enc, u_dec, i_dec) = \
        temporal_holdout_split(interactions, alpha=40)

    u = u_enc["user1"]
    assert i_dec[test_items[u]] == "vodA"          # 최신 시각 아이템
    # 홀드아웃은 학습셋에서 제거
    assert train_mat[u, i_enc["vodA"]] == 0
    # 나머지는 학습셋에 유지
    assert train_mat[u, i_enc["vodB"]] > 0
    assert train_mat[u, i_enc["vodC"]] > 0
    # full_mat 은 3건 모두 포함
    assert full_mat.getrow(u).nnz == 3


def test_temporal_excludes_min_history_users():
    """서로 다른 아이템이 1개뿐인 유저는 평가 대상에서 제외된다."""
    interactions = [
        ("solo", "vodA", 0.5, _ts(0)),           # 이력 1개 → 제외
        ("ok", "vodA", 0.5, _ts(0)),
        ("ok", "vodB", 0.5, _ts(1)),
    ]
    _, test_items, _, (u_enc, *_rest) = temporal_holdout_split(interactions)
    assert u_enc["solo"] not in test_items
    assert u_enc["ok"] in test_items


def test_temporal_repeat_watch_not_leaked():
    """동일 VOD 반복 시청(과거+최신) 시, 그 VOD가 홀드아웃이면 학습셋에 남지 않아야 한다."""
    interactions = [
        ("u", "vodA", 0.3, _ts(0)),   # 과거
        ("u", "vodA", 0.9, _ts(5)),   # 최신(동일 VOD)
        ("u", "vodB", 0.5, _ts(1)),
    ]
    train_mat, test_items, full_mat, (u_enc, i_enc, *_r) = temporal_holdout_split(interactions)
    u = u_enc["u"]
    assert i_enc["vodA"] == test_items[u]
    assert train_mat[u, i_enc["vodA"]] == 0        # 누수 없음
    assert full_mat.getrow(u).nnz == 2             # (vodA, vodB) 집계


def test_temporal_tie_break_deterministic():
    """최신 시각이 동률이면 item_idx 가 큰 쪽을 결정적으로 선택."""
    interactions = [
        ("u", "vodA", 0.5, _ts(3)),
        ("u", "vodB", 0.5, _ts(3)),   # 동률
        ("u", "vodC", 0.5, _ts(1)),
    ]
    _, test_items, _, (u_enc, i_enc, i_dec_rev, i_dec) = temporal_holdout_split(interactions)
    u = u_enc["u"]
    # vodA, vodB 중 인덱스 큰 쪽 → 사전순 뒤인 vodB
    assert i_dec[test_items[u]] == "vodB"


def test_temporal_confidence_uses_max():
    interactions = [
        ("u", "vodA", 0.2, _ts(0)),
        ("u", "vodA", 0.8, _ts(1)),   # 최신이자 최대 confidence
        ("u", "vodB", 0.5, _ts(2)),
    ]
    train_mat, test_items, full_mat, (u_enc, i_enc, *_r) = temporal_holdout_split(interactions, alpha=40)
    u = u_enc["u"]
    # vodB 가 홀드아웃(day2). vodA 는 학습셋에 max confidence=1+40*0.8 로
    assert abs(train_mat[u, i_enc["vodA"]] - (1 + 40 * 0.8)) < 1e-4


# ── random 분리 ──────────────────────────────────────────────────────

def test_random_split_reproducible():
    mat = csr_matrix(np.array([
        [1, 2, 3, 0],
        [0, 1, 1, 1],
    ], dtype=np.float32))
    t1, items1 = random_holdout_split(mat, seed=7)
    t2, items2 = random_holdout_split(mat, seed=7)
    assert items1 == items2                        # 동일 시드 → 동일 결과
    # 홀드아웃이 학습셋에서 제거됨
    for u, i in items1.items():
        assert t1[u, i] == 0


def test_random_split_excludes_single_item_user():
    mat = csr_matrix(np.array([
        [5, 0, 0],      # 이력 1개 → 제외
        [1, 1, 0],
    ], dtype=np.float32))
    _, items = random_holdout_split(mat, seed=1)
    assert 0 not in items
    assert 1 in items


# ── 지표 ─────────────────────────────────────────────────────────────

def test_ndcg_rank1_is_1():
    assert ndcg_at_k([9, 1, 2], relevant=9, k=10) == pytest.approx(1.0)


def test_ndcg_rank2():
    # rank2 → 1/log2(3)
    assert ndcg_at_k([1, 9, 2], relevant=9, k=10) == pytest.approx(1.0 / np.log2(3))


def test_ndcg_miss_is_0():
    assert ndcg_at_k([1, 2, 3], relevant=9, k=10) == 0.0


def test_reciprocal_rank():
    assert reciprocal_rank([1, 2, 9], 9, 10) == pytest.approx(1 / 3)
    assert reciprocal_rank([1, 2, 3], 9, 10) == 0.0


def test_hit_respects_cutoff():
    assert hit([1, 2, 3, 9], 9, k=3) == 0
    assert hit([1, 2, 3, 9], 9, k=4) == 1


def test_evaluate_rankings_overall_and_segments():
    rec = {0: [9, 1, 2], 1: [5, 6, 7]}
    rel = {0: 9, 1: 7}
    hist = {0: 3, 1: 25}   # cold, hot
    out = evaluate_rankings(rec, rel, k=3, n_items=10, history_by_user=hist)
    o = out["overall"]
    assert o["HitRate@3"] == pytest.approx(1.0)        # 둘 다 적중
    assert o["NDCG@3"] == pytest.approx((1.0 + 1.0 / np.log2(4)) / 2)
    assert o["eval_users"] == 2
    assert 0.0 < o["Coverage"] <= 1.0
    assert out["segments"]["cold (2~4)"]["users"] == 1
    assert out["segments"]["hot (20+)"]["users"] == 1
