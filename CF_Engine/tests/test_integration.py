"""tests/test_integration.py — 데이터 로드→추천→포매팅→Fallback→DB 적재 통합 경로 (P1-3).

implicit·실DB 없이 전 구간을 연결한다. ALS 학습 자체는 implicit 의존이므로
'ALS 추천 출력'(item_indices/scores)만 주입해 그 이후 전 구간을 검증한다.
"""

import sys

import numpy as np
import pytest

sys.path.insert(0, ".")

from CF_Engine.src.data_loader import DataLoader
from CF_Engine.src.recommender import Recommender
import CF_Engine.src.db_writer as dbw

build_records = Recommender.build_records
apply_fallback = Recommender.apply_fallback


# ── watch_history 조회 stub ──────────────────────────────────────────

def _conn_stub(rows):
    class Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return rows
        def close(self): pass
    class Conn:
        def cursor(self): return Cur()
    return Conn()


# ── DB 적재용 FakeConn ───────────────────────────────────────────────

class FakeCursor:
    def __init__(self, store, calls):
        self.store, self.calls, self.rowcount = store, calls, 0
    def execute(self, sql, params=None):
        if "DELETE" in sql:
            rtype, users = params[0], params[1]
            before = len(self.store)
            self.store[:] = [r for r in self.store
                             if not (r["recommendation_type"] == rtype and r["user_id_fk"] in users)]
            self.rowcount = before - len(self.store)
            self.calls.append("DELETE")
    def close(self): pass


class FakeConn:
    def __init__(self):
        self.store, self.calls = [], []
        self.committed = self.rolled_back = 0
    def cursor(self): return FakeCursor(self.store, self.calls)
    def commit(self): self.committed += 1
    def rollback(self): self.rolled_back += 1


@pytest.fixture
def patch_batch(monkeypatch):
    def fake(cur, sql, batch, page_size=None):
        cur.calls.append("INSERT")
        for r in batch:
            cur.store.append(dict(r))
    monkeypatch.setattr(dbw, "execute_batch", fake)


# ── 통합 테스트 ──────────────────────────────────────────────────────

def test_full_pipeline_data_to_db(patch_batch):
    # 1) 데이터 로드: user1 은 vodX 만 시청. user2 가 A1,A2,C,X 시청(아이템 학습셋 형성).
    rows = [
        ("user1", "vodX", 0.9),
        ("user2", "vodA1", 0.9),
        ("user2", "vodA2", 0.8),
        ("user2", "vodC", 0.7),
        ("user2", "vodX", 0.6),
    ]
    mat, u_enc, i_enc, u_dec, i_dec = DataLoader().load_matrix(_conn_stub(rows), alpha=40)
    assert mat.shape == (2, 4)                     # user 2명 × item 4개(A1,A2,C,X)

    # 2) ALS 추천 출력 주입(이미 시청한 것은 제외된 상태 — filter_already_liked_items 계약)
    #    user1 은 vodX 만 시청 → A1,A2(동일 시리즈),C 추천(미시청)
    vod_series_map = {
        "vodA1": ("SeriesA", "영화"),
        "vodA2": ("SeriesA", "영화"),   # 같은 시리즈 → 중복 제거 대상
        "vodC": ("SeriesC", "영화"),
        "vodX": ("SeriesX", "영화"),
    }
    # user1: [A1, A2, C] → 시리즈 중복 제거 후 [A1, C]
    user_ids = [u_enc["user1"], u_enc["user2"]]
    item_indices = np.array([
        [i_enc["vodA1"], i_enc["vodA2"], i_enc["vodC"]],
        [i_enc["vodX"], i_enc["vodX"], i_enc["vodX"]],
    ])
    scores = np.array([[0.9, 0.8, 0.7], [0.6, 0.6, 0.6]])

    # 3) 포매팅 + 시리즈 중복 제거
    records = build_records(user_ids, item_indices, scores, u_dec, i_dec,
                            recommendation_type="COLLABORATIVE", top_k=20,
                            vod_series_map=vod_series_map)
    u1 = [r for r in records if r["user_id_fk"] == "user1"]
    u1_vods = [r["vod_id_fk"] for r in u1]
    assert u1_vods == ["vodA1", "vodC"]           # 같은 시리즈 A2 제거
    assert "vodX" not in u1_vods                  # 이미 시청한 콘텐츠 제외(ALS 계약 유지)
    # recommendation_type 계약: 모든 레코드 COLLABORATIVE
    assert all(r["recommendation_type"] == "COLLABORATIVE" for r in records)
    # 랭크 연속성
    assert [r["rank"] for r in u1] == [1, 2]

    # 4) DB 적재
    conn = FakeConn()
    stats = dbw.export(conn, records, batch_size=100, recommendation_type="COLLABORATIVE")
    assert conn.committed == 1
    assert stats["inserted"] == len(records)
    assert all(r["recommendation_type"] == "COLLABORATIVE" for r in conn.store)
    # DELETE 먼저, 그 다음 INSERT
    assert conn.calls.index("DELETE") < conn.calls.index("INSERT")


def test_top_k_cap_enforced(patch_batch):
    """시리즈가 모두 다르면 Top-20 로 상한이 지켜진다."""
    item_dec = {i: f"v{i}" for i in range(30)}
    user_dec = {0: "u1"}
    series_map = {f"v{i}": (f"S{i}", "영화") for i in range(30)}   # 전부 다른 시리즈
    item_indices = np.array([list(range(30))])
    scores = np.array([[1.0 - i * 0.01 for i in range(30)]])
    records = build_records([0], item_indices, scores, user_dec, item_dec,
                            recommendation_type="COLLABORATIVE", top_k=20,
                            vod_series_map=series_map)
    assert len(records) == 20                     # Top-20 확보/상한
    assert records[-1]["rank"] == 20


def test_underfilled_user_filled_then_loaded(patch_batch):
    """추천 부족 유저가 Fallback 으로 채워진 뒤 DB 에 적재되는 전 구간."""
    # user1: ALS 1건만(vodA) → top_k=3 미달
    records = [{"user_id_fk": "u1", "vod_id_fk": "vodA", "rank": 1,
                "score": 0.9, "recommendation_type": "COLLABORATIVE"}]
    records, fb = apply_fallback(records, ["popX", "popY"], top_k=3,
                                 recommendation_type="COLLABORATIVE",
                                 watched_by_user={"u1": {"vodW"}})
    assert fb["items_added"] == 2
    conn = FakeConn()
    dbw.export(conn, records, recommendation_type="COLLABORATIVE")
    stored = sorted(r["vod_id_fk"] for r in conn.store)
    assert stored == ["popX", "popY", "vodA"]
    assert all(r["recommendation_type"] == "COLLABORATIVE" for r in conn.store)


def test_db_failure_propagates(monkeypatch):
    """적재 중 외부(DB) 실패 시 롤백되고 예외가 호출부로 전파된다."""
    def boom(cur, sql, batch, page_size=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(dbw, "execute_batch", boom)

    records = [{"user_id_fk": "u1", "vod_id_fk": "vodA", "rank": 1,
                "score": 0.9, "recommendation_type": "COLLABORATIVE"}]
    conn = FakeConn()
    with pytest.raises(RuntimeError):
        dbw.export(conn, records)
    assert conn.rolled_back == 1
    assert conn.committed == 0
    assert conn.store == []                        # 부분 적재 없음
