"""tests/test_fallback.py — Cold 사용자 Fallback 및 추천 부족 대응 (P1-1).

DB 없이 순수 함수(apply_fallback)만 검증한다.
"""

import sys

import pytest

sys.path.insert(0, ".")

from CF_Engine.src.recommender import Recommender

apply_fallback = Recommender.apply_fallback

POPULAR = ["p1", "p2", "p3", "p4", "p5"]


def _rec(uid, vod, rank, score=0.9, rtype="COLLABORATIVE"):
    return {"user_id_fk": uid, "vod_id_fk": vod, "rank": rank,
            "score": score, "recommendation_type": rtype}


def test_underfilled_user_gets_filled_to_top_k():
    records = [_rec("u1", "a", 1), _rec("u1", "b", 2)]   # 2건 → top_k=5 미달
    out, stats = apply_fallback(records, POPULAR, top_k=5)
    u1 = [r for r in out if r["user_id_fk"] == "u1"]
    assert len(u1) == 5
    # 랭크 연속성
    assert [r["rank"] for r in u1] == [1, 2, 3, 4, 5]
    assert stats["items_added"] == 3
    assert stats["users_filled"] == 1


def test_no_duplicate_with_existing_recs():
    records = [_rec("u1", "p1", 1)]       # 이미 p1 추천됨
    out, _ = apply_fallback(records, POPULAR, top_k=3)
    vods = [r["vod_id_fk"] for r in out]
    assert vods.count("p1") == 1          # 중복 없음
    assert vods == ["p1", "p2", "p3"]


def test_zero_rec_cold_user_created_from_all_user_ids():
    records = []                          # ALS 추천 0건
    out, stats = apply_fallback(records, POPULAR, top_k=3,
                                all_user_ids=["cold1", "cold2"])
    assert stats["users_zero_before"] == 2
    assert len([r for r in out if r["user_id_fk"] == "cold1"]) == 3
    assert len([r for r in out if r["user_id_fk"] == "cold2"]) == 3


def test_watched_items_excluded():
    records = [_rec("u1", "a", 1)]
    watched = {"u1": {"p1", "p2"}}        # p1,p2 이미 시청 → 제외
    out, _ = apply_fallback(records, POPULAR, top_k=3, watched_by_user=watched)
    vods = [r["vod_id_fk"] for r in out if r["user_id_fk"] == "u1"]
    assert "p1" not in vods and "p2" not in vods
    assert vods == ["a", "p3", "p4"]


def test_series_dedup_in_fallback():
    # p1,p2 는 같은 시리즈 → 하나만 채워야 함
    vod_series_map = {
        "p1": ("SeriesX", "영화"),
        "p2": ("SeriesX", "영화"),
        "p3": ("SeriesY", "영화"),
    }
    records = [_rec("u1", "a", 1)]
    out, _ = apply_fallback(records, ["p1", "p2", "p3"], top_k=5,
                            vod_series_map=vod_series_map)
    vods = [r["vod_id_fk"] for r in out if r["user_id_fk"] == "u1"]
    assert "p1" in vods and "p2" not in vods      # 같은 시리즈 중복 제거
    assert "p3" in vods


def test_episode_level_ct_cl_not_series_deduped():
    # 'TV 연예/오락' 은 에피소드 단위 유지 → 같은 series_nm 여도 둘 다 채움
    vod_series_map = {
        "p1": ("쇼프로", "TV 연예/오락"),
        "p2": ("쇼프로", "TV 연예/오락"),
    }
    records = [_rec("u1", "a", 1)]
    out, _ = apply_fallback(records, ["p1", "p2"], top_k=5,
                            vod_series_map=vod_series_map)
    vods = [r["vod_id_fk"] for r in out if r["user_id_fk"] == "u1"]
    assert "p1" in vods and "p2" in vods


def test_already_full_user_untouched():
    records = [_rec("u1", f"v{i}", i) for i in range(1, 6)]   # 5건 = top_k
    out, stats = apply_fallback(records, POPULAR, top_k=5)
    assert len(out) == 5
    assert stats["items_added"] == 0
    assert stats["users_filled"] == 0


def test_still_short_when_not_enough_popular():
    records = [_rec("u1", "a", 1)]
    out, stats = apply_fallback(records, ["p1"], top_k=5)   # 후보 1개뿐
    u1 = [r for r in out if r["user_id_fk"] == "u1"]
    assert len(u1) == 2                 # a + p1
    assert stats["users_still_short_after"] == 1


def test_fallback_score_and_type_preserved():
    records = [_rec("u1", "a", 1)]
    out, _ = apply_fallback(records, ["p1"], top_k=2,
                            recommendation_type="COLLABORATIVE", fallback_score=0.0)
    fb = [r for r in out if r["vod_id_fk"] == "p1"][0]
    assert fb["score"] == 0.0
    assert fb["recommendation_type"] == "COLLABORATIVE"
