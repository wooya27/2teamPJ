"""tests/test_db_writer.py — DB 적재 안정성·장애 복구 (P1-2).

실제 DB 없이 FakeConn 으로 DELETE/INSERT 시맨틱을 시뮬레이션하고,
execute_batch 를 monkeypatch 해 트랜잭션·재실행·롤백 동작을 검증한다.
"""

import sys

import pytest

sys.path.insert(0, ".")

import CF_Engine.src.db_writer as dbw


class FakeCursor:
    """serving.vod_recommendation 을 흉내내는 인메모리 커서."""

    def __init__(self, store, calls, fail_on_insert=False):
        self.store = store        # list[dict] — 적재된 행
        self.calls = calls        # list[str] — 실행 순서 기록 ("DELETE"/"INSERT"/...)
        self.rowcount = 0
        self._fail_on_insert = fail_on_insert
        self._inserted_this_exec = False

    def execute(self, sql, params=None):
        if "DELETE" in sql and "NOT (" in sql:
            rtype, users, keep_vods = params
            before = len(self.store)
            self.store[:] = [r for r in self.store
                             if not (r["recommendation_type"] == rtype
                                     and r["user_id_fk"] in users
                                     and r["vod_id_fk"] not in keep_vods)]
            self.rowcount = before - len(self.store)
            self.calls.append("DELETE_STALE")
        elif "DELETE" in sql:
            rtype, users = params
            before = len(self.store)
            self.store[:] = [r for r in self.store
                             if not (r["recommendation_type"] == rtype
                                     and r["user_id_fk"] in users)]
            self.rowcount = before - len(self.store)
            self.calls.append("DELETE")
        elif "INSERT" in sql and "CONFLICT" in sql:
            self.calls.append("UPSERT")
        elif "INSERT" in sql:
            self.calls.append("INSERT")

    def close(self):
        pass


class FakeConn:
    def __init__(self, store=None, fail_on_insert=False):
        self.store = store if store is not None else []
        self.calls = []
        self.committed = 0
        self.rolled_back = 0
        self._fail_on_insert = fail_on_insert

    def cursor(self):
        return FakeCursor(self.store, self.calls, self._fail_on_insert)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture
def patch_execute_batch(monkeypatch):
    """execute_batch → FakeCursor.store 에 직접 삽입하도록 대체.

    upsert 는 conflict key(user,vod,type) 로 중복을 UPDATE 하므로 그 시맨틱도 반영.
    """
    def fake(cur, sql, batch, page_size=None):
        if cur._fail_on_insert:
            raise RuntimeError("simulated insert failure")
        is_upsert = "CONFLICT" in sql
        cur.calls.append("UPSERT" if is_upsert else "INSERT")
        for r in batch:
            if is_upsert:
                for existing in cur.store:
                    if (existing["user_id_fk"] == r["user_id_fk"]
                            and existing["vod_id_fk"] == r["vod_id_fk"]
                            and existing["recommendation_type"] == r["recommendation_type"]):
                        existing["rank"] = r["rank"]
                        existing["score"] = r["score"]
                        break
                else:
                    cur.store.append(dict(r))
            else:
                cur.store.append(dict(r))
    monkeypatch.setattr(dbw, "execute_batch", fake)


def _records(uid="u1", vods=("a", "b"), rtype="COLLABORATIVE"):
    return [{"user_id_fk": uid, "vod_id_fk": v, "rank": i + 1,
             "score": 0.9 - i * 0.1, "recommendation_type": rtype}
            for i, v in enumerate(vods)]


# ── DELETE + INSERT ──────────────────────────────────────────────────

def test_delete_before_insert(patch_execute_batch):
    conn = FakeConn()
    dbw.export(conn, _records(), batch_size=10)
    assert conn.calls.index("DELETE") < conn.calls.index("INSERT")
    assert conn.committed == 1


def test_rerun_is_idempotent(patch_execute_batch):
    """동일 배치를 두 번 적재해도 중복 없이 같은 상태."""
    recs = _records("u1", ("a", "b", "c"))
    conn = FakeConn()
    dbw.export(conn, [dict(r) for r in recs])
    state1 = sorted((r["user_id_fk"], r["vod_id_fk"]) for r in conn.store)
    dbw.export(conn, [dict(r) for r in recs])
    state2 = sorted((r["user_id_fk"], r["vod_id_fk"]) for r in conn.store)
    assert state1 == state2
    assert len(conn.store) == 3          # 중복 누적 없음


def test_rerun_removes_stale_recs(patch_execute_batch):
    """이전 추천(a,b,c) → 신규(a,d) 재실행 시 b,c 가 남지 않아야 한다."""
    conn = FakeConn()
    dbw.export(conn, _records("u1", ("a", "b", "c")))
    dbw.export(conn, _records("u1", ("a", "d")))
    vods = sorted(r["vod_id_fk"] for r in conn.store)
    assert vods == ["a", "d"]


def test_insert_failure_rolls_back_and_raises(patch_execute_batch):
    conn = FakeConn(fail_on_insert=True)
    with pytest.raises(RuntimeError):
        dbw.export(conn, _records())
    assert conn.rolled_back == 1
    assert conn.committed == 0


def test_empty_records_noop(patch_execute_batch):
    conn = FakeConn()
    stats = dbw.export(conn, [])
    assert stats["inserted"] == 0
    assert conn.committed == 0 and conn.rolled_back == 0


def test_score_clipped_to_one(patch_execute_batch):
    conn = FakeConn()
    recs = [{"user_id_fk": "u1", "vod_id_fk": "a", "rank": 1,
             "score": 1.0000012, "recommendation_type": "COLLABORATIVE"}]
    dbw.export(conn, recs)
    assert conn.store[0]["score"] <= 1.0


def test_stats_counts(patch_execute_batch):
    conn = FakeConn()
    stats = dbw.export(conn, _records("u1", ("a", "b")))
    assert stats["inserted"] == 2
    assert stats["users"] == 1
    assert stats["method"] == "delete_insert"


# ── UPSERT ───────────────────────────────────────────────────────────

def test_upsert_updates_in_place_no_duplicate(patch_execute_batch):
    conn = FakeConn()
    dbw.export_upsert(conn, _records("u1", ("a", "b")))
    dbw.export_upsert(conn, _records("u1", ("a", "b")))   # 재실행
    assert len(conn.store) == 2          # 중복 생성 없음(제자리 UPDATE)


def test_upsert_prunes_stale(patch_execute_batch):
    conn = FakeConn()
    dbw.export_upsert(conn, _records("u1", ("a", "b", "c")))
    dbw.export_upsert(conn, _records("u1", ("a", "d")))   # b,c 는 stale
    vods = sorted(r["vod_id_fk"] for r in conn.store)
    assert vods == ["a", "d"]
    assert "DELETE_STALE" in conn.calls


def test_upsert_failure_rolls_back(patch_execute_batch):
    conn = FakeConn(fail_on_insert=True)
    with pytest.raises(RuntimeError):
        dbw.export_upsert(conn, _records())
    assert conn.rolled_back == 1
