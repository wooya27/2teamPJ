"""tests/test_fair_compare.py — 공정 코호트 정렬 로직 (P0-3).

implicit·DB 없이 순수 함수만 검증한다.
"""

import sys
from datetime import datetime, timedelta

import numpy as np
import pytest
from scipy.sparse import csr_matrix

sys.path.insert(0, ".")

from CF_Engine.src.evaluation import (
    restrict_matrix_columns,
    align_fair_cohort,
)


def _ts(d):
    return datetime(2026, 1, 1) + timedelta(days=d)


def test_restrict_zeroes_non_quality_columns():
    mat = csr_matrix(np.array([
        [1, 2, 3],
        [4, 0, 5],
    ], dtype=np.float32))
    out = restrict_matrix_columns(mat, keep_cols={0, 2})
    assert out.shape == mat.shape
    # 유지된 컬럼
    assert out[0, 0] == 1 and out[0, 2] == 3
    assert out[1, 0] == 4 and out[1, 2] == 5
    # 제외 컬럼
    assert out[0, 1] == 0 and out[1, 1] == 0


def test_align_excludes_users_with_filtered_holdout():
    """홀드아웃(최신 시청) 정답이 품질 아이템이 아니면 공정 평가에서 제외."""
    interactions = [
        # userA: 최신=vodBad(비품질) → 제외 대상
        ("userA", "vodGood", 0.5, _ts(0)),
        ("userA", "vodBad", 0.9, _ts(3)),
        # userB: 최신=vodGood2(품질) → 포함
        ("userB", "vodGood", 0.5, _ts(0)),
        ("userB", "vodGood2", 0.9, _ts(3)),
    ]
    quality = {"vodGood", "vodGood2"}
    res = align_fair_cohort(interactions, quality, alpha=40)

    u_enc = res["encoders"][0]
    fair = res["test_items_fair"]
    assert u_enc["userA"] not in fair
    assert u_enc["userB"] in fair
    assert res["stats"]["eval_users_all"] == 2
    assert res["stats"]["eval_users_fair"] == 1
    assert res["stats"]["excluded_holdout_filtered"] == 1


def test_align_on_model_has_no_mass_on_nonquality():
    interactions = [
        ("u", "vodGood", 0.5, _ts(0)),
        ("u", "vodBad", 0.5, _ts(1)),
        ("u", "vodGood2", 0.9, _ts(2)),   # 최신=품질 → 평가 포함
        ("v", "vodGood", 0.5, _ts(0)),
        ("v", "vodGood2", 0.9, _ts(2)),
    ]
    quality = {"vodGood", "vodGood2"}
    res = align_fair_cohort(interactions, quality, alpha=40)

    i_enc = res["encoders"][1]
    bad_col = i_enc["vodBad"]
    train_on = res["train_on"]
    # ON 학습행렬엔 비품질 컬럼 질량이 0
    assert train_on[:, bad_col].sum() == 0
    # OFF 학습행렬엔 남아있을 수 있음 (u 의 vodBad 는 홀드아웃이 아니므로 학습셋에 존재)
    train_off = res["train_off"]
    assert train_off[:, bad_col].sum() > 0
    # 동일 shape
    assert train_on.shape == train_off.shape


def test_align_returns_quality_cols_for_candidate_restriction():
    """ON 모델 추천 후보 제한에 쓸 품질 컬럼 인덱스를 반환한다."""
    interactions = [
        ("u", "a", 0.5, _ts(0)),
        ("u", "b", 0.5, _ts(1)),
        ("u", "c", 0.9, _ts(2)),
    ]
    quality = {"a", "c"}
    res = align_fair_cohort(interactions, quality, alpha=40)
    i_enc = res["encoders"][1]
    assert res["quality_cols"] == sorted([i_enc["a"], i_enc["c"]])


def test_align_stats_item_counts():
    interactions = [
        ("u", "a", 0.5, _ts(0)),
        ("u", "b", 0.5, _ts(1)),
        ("u", "c", 0.9, _ts(2)),
    ]
    quality = {"a", "b"}            # c 는 비품질
    res = align_fair_cohort(interactions, quality, alpha=40)
    assert res["stats"]["n_items_total"] == 3
    assert res["stats"]["n_items_quality"] == 2
