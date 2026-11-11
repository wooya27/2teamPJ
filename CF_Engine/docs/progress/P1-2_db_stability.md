# 중간보고서 — P1-2 DB 적재 안정성 및 장애 복구

> 대상: `CF_Engine/scripts/export_to_db.py`, `CF_Engine/src/db_writer.py`, `scripts/train.py`
> 작업계획: `CF_Engine_11.md` §2 P1-2
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료**

## 1. 문제 (Before)

기존 `export()` 는 DELETE + 배치 INSERT 후 마지막에 `commit()` 1회였으나:

- **예외 처리 없음**: INSERT 배치 중간 실패 시 `rollback()` 호출이 없어 커넥션이
  aborted 트랜잭션 상태로 남는다("실패하면 무엇이 남는가?" 에 답 불가).
- **테스트 불가 위치**: 로직이 `scripts/` 에 있어 pytest 대상이 아니었다.
- UPSERT 등 대안과의 비교·근거가 코드/문서에 없었다.

## 2. 변경 (After)

### 2.1 로직을 `src/db_writer.py` 로 이동(테스트 가능)

`scripts/export_to_db.py` 는 `from src.db_writer import export, export_upsert` 를
재노출하는 **얇은 래퍼**로 축소 → `train.py` 의 `from export_to_db import export`
하위호환 유지(CLAUDE.md: src=라이브러리, scripts=실행).

### 2.2 `export()` — DELETE + INSERT, 단일 트랜잭션 + 롤백

```python
try:
    cur.execute(DELETE_SQL, (type, user_ids))      # 해당 type·user 삭제
    for batch in ...: execute_batch(cur, INSERT_SQL, batch)
    conn.commit()                                   # DELETE+INSERT 원자적 커밋
except Exception as e:
    conn.rollback(); log.error(...); raise          # 실패 시 삽입 0건 유지
finally:
    cur.close()
```

- **재실행 안전(idempotent)**: 해당 `recommendation_type`·user 삭제 후 재삽입 →
  몇 번을 돌려도 중복 누적 없음.
- **stale 제거**: 이전 추천(a,b,c) → 신규(a,d) 재실행 시 b,c 가 자동 제거.
- **stats 반환**: `deleted/inserted/users/elapsed_sec/method` → 건수·시간 기록.

### 2.3 `export_upsert()` — 대안(ON CONFLICT) + stale prune

```sql
INSERT ... ON CONFLICT (user_id_fk, vod_id_fk, recommendation_type)
DO UPDATE SET rank = EXCLUDED.rank, score = EXCLUDED.score
```

변경 없는 추천은 삭제·재삽입 없이 **제자리 UPDATE** → 조회 측에서 추천이 비는
구간(DELETE→INSERT 사이)이 없다. 신규 집합에 없는 stale 행은 `DELETE_STALE_SQL`
로 별도 정리. CLAUDE.md 가 명시한 `UNIQUE(user_id_fk, vod_id_fk, recommendation_type)`
제약을 conflict key 로 사용.

### 2.4 방식 비교 (계획서 체크 3)

| 기준 | DELETE + INSERT (기본) | UPSERT + prune |
|---|---|---|
| 구현 복잡도 | 낮음 | 중간(stale prune 필요) |
| stale 제거 | 자동(삭제 후 재삽입) | 별도 DELETE 필요 |
| 조회 빈 구간 | DELETE~INSERT 사이 존재(단일 txn이라 커밋 전까지 외부 미노출) | 없음(제자리 UPDATE) |
| 재실행 안전성 | ✅ | ✅ |
| 대량 쓰기 비용 | 전량 재삽입 | 변경분만 UPDATE(유리) |

→ 현재 규모/단순성에서는 **DELETE+INSERT 를 기본**으로, 조회 지연·대량 UPDATE가
문제가 되면 UPSERT 전환(코드 이미 제공).

## 3. 검증 (테스트)

`CF_Engine/tests/test_db_writer.py` 신규 10케이스 — FakeConn 으로 DELETE/INSERT
시맨틱을 시뮬레이션하고 `execute_batch` 를 monkeypatch 해 **통과**.

```
$ python -m pytest CF_Engine/tests/test_db_writer.py -q
10 passed in 0.05s
```

- DELETE 가 INSERT 보다 먼저 실행, commit 1회 ✔
- **동일 배치 2회 재실행 → 중복 없음**(계획서 체크 1) ✔
- 재실행 시 stale(b,c) 제거 ✔
- **INSERT 실패 → rollback 호출 + 예외 재-raise, commit 0회**(계획서 체크 2) ✔
- 빈 레코드 no-op / score 1.0 클리핑 / stats 카운트 ✔
- UPSERT 재실행 중복 없음 / stale prune / 실패 롤백 ✔

## 4. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 재실행 중복 없음 확인 | ✅ 테스트 |
| 중간 실패 시 트랜잭션 처리 | ✅ rollback + 재-raise |
| DELETE+INSERT vs UPSERT 장단점 비교 | ✅ 표 + 양쪽 구현 |
| 건수·시간·에러 로그 | ✅ stats + log |
| "실패하면 무엇이 남고 어떻게 복구?" | ✅ 삽입 0건 유지, 재실행으로 복구 |

## 5. 한계 / 다음 블록

- 대규모 메모리·처리 시간 **실측**(계획서 마지막 체크)은 DB 재실행 시 `elapsed_sec`
  와 배치 로그로 확보. 코드상 레코드를 메모리에 전량 구성 후 배치 INSERT 하므로,
  유저 수가 매우 커지면 레코드 제너레이터/청크 스트리밍으로 전환 여지 있음(후속).
- 다음: **P1-3 통합 테스트 및 API 연계 검증**.

## 변경 파일

- `CF_Engine/src/db_writer.py` (신규: `export`, `export_upsert`)
- `CF_Engine/scripts/export_to_db.py` (얇은 래퍼로 축소, 하위호환 재노출)
- `CF_Engine/tests/test_db_writer.py` (신규, 10 케이스)
