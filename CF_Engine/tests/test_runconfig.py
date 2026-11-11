"""tests/test_runconfig.py — 품질 필터 설정 해석 및 필터 SQL 구조 검증 (P0-2)."""

import sys

import pytest

sys.path.insert(0, ".")

from CF_Engine.src.runconfig import resolve_filter_quality, describe_run
from CF_Engine.src.data_loader import DataLoader


# ── 설정 해석 우선순위 ───────────────────────────────────────────────

def test_default_false():
    assert resolve_filter_quality({}) is False
    assert resolve_filter_quality({"data": {}}) is False


def test_yaml_true():
    assert resolve_filter_quality({"data": {"filter_quality": True}}) is True


def test_cli_overrides_yaml_off():
    cfg = {"data": {"filter_quality": True}}
    assert resolve_filter_quality(cfg, cli_no_filter=True) is False


def test_cli_overrides_yaml_on():
    cfg = {"data": {"filter_quality": False}}
    assert resolve_filter_quality(cfg, cli_filter=True) is True


def test_conflicting_cli_raises():
    with pytest.raises(ValueError):
        resolve_filter_quality({}, cli_filter=True, cli_no_filter=True)


def test_describe_run_includes_resolved_filter():
    cfg = {"model": {"factors": 128, "alpha": 40}, "recommend": {"top_k": 20}}
    d = describe_run(cfg, filter_quality=True, dry_run=True)
    assert d["filter_quality"] is True
    assert d["dry_run"] is True
    assert d["factors"] == 128


# ── 필터 SQL 구조: poster_url AND vod_embedding 둘 다 요구 ────────────

class _CaptureCursor:
    def __init__(self, store):
        self._store = store
    def execute(self, sql, *a, **kw):
        self._store.append(sql)
    def fetchall(self):
        # 비어있지 않은 결과 1행(빈 행렬 division-by-zero 회피). load_interactions 는
        # 4-튜플을 기대하지만 SQL 캡처만 확인하므로 추가 원소는 무시된다.
        return [("u1", "v1", 0.5, None)]
    def close(self):
        pass


class _CaptureConn:
    def __init__(self):
        self.sqls = []
    def cursor(self):
        return _CaptureCursor(self.sqls)


def test_filter_matrix_sql_requires_poster_and_embedding():
    conn = _CaptureConn()
    DataLoader().load_matrix(conn, alpha=40, filter_quality=True)
    sql = conn.sqls[0]
    assert "poster_url" in sql
    assert "vod_embedding" in sql
    assert "is_test = FALSE" in sql


def test_nofilter_matrix_sql_has_no_embedding_join():
    conn = _CaptureConn()
    DataLoader().load_matrix(conn, alpha=40, filter_quality=False)
    sql = conn.sqls[0]
    assert "vod_embedding" not in sql


def test_filter_interactions_sql_requires_poster_and_embedding_and_strt_dt():
    conn = _CaptureConn()
    DataLoader().load_interactions(conn, filter_quality=True)
    sql = conn.sqls[0]
    assert "poster_url" in sql
    assert "vod_embedding" in sql
    assert "strt_dt" in sql          # 시간순 분리에 필요한 컬럼 포함
