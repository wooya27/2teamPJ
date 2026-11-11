# 중간보고서 — P0-1 평가 데이터 분리 방식 개선

> 대상: `CF_Engine/scripts/evaluate.py`, `CF_Engine/scripts/full_eval.py`
> 작업계획: `CF_Engine_11.md` §2 P0-1
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료 / 지표 재측정은 DB 재실행 시**

## 1. 문제 (Before)

기존 `split_holdout()` 은 유저별 홀드아웃을 CSR 행렬의 비영 아이템 인덱스 중
`nz[-1]` 로 선택했다.

```python
nz = row.nonzero()[1]     # 정렬된 '컬럼 인덱스' 배열
hold = int(nz[-1])        # 최대 컬럼 인덱스 — 최신 시청이 아님
```

`nz` 는 `scipy` 가 반환하는 **정렬된 컬럼(item) 인덱스**다. 따라서 `nz[-1]` 은
"가장 최근에 본 VOD" 가 아니라 "item_encoder 상 인덱스가 가장 큰 VOD" 일 뿐이며,
인코더 인덱스는 `vod_id` 사전순으로 부여되므로 시청 시각과 무관하다. 즉 시간순
Leave-last-out 을 의도했으나 실제로는 **임의(사전순 최대) 아이템**을 평가 정답으로
쓰고 있었다 → NDCG/HitRate 의 신뢰성 훼손.

## 2. 근거: 시간 컬럼 존재 확인

`Database_Design/schemas/create_tables.sql` (SSoT, 루트 CLAUDE.md Rule 1):

```sql
CREATE TABLE watch_history (
    ...
    strt_dt   TIMESTAMPTZ  NOT NULL,   -- 시청 시작 시각 (신뢰 가능)
    ...
);
```

`strt_dt` 가 `NOT NULL` 로 보장되므로 **시간순 분리를 실제로 구현 가능**하다.
난수 시드 대안은 보조로만 둔다.

## 3. 변경 (After)

### 3.1 평가 로직을 테스트 가능한 `src/` 모듈로 분리

`scripts/` 는 pytest 대상이 아니므로(CLAUDE.md 폴더 규칙), 분리·지표 로직을
**순수 함수 모듈 `CF_Engine/src/evaluation.py`** 로 추출했다. DB·implicit 의존이
없어 단위 테스트에서 그대로 호출된다.

| 함수 | 역할 |
|------|------|
| `temporal_holdout_split(interactions, alpha)` | strt_dt 기준 유저별 최신 1건 분리 (권장) |
| `random_holdout_split(mat, seed)` | 고정 시드 무작위 분리 (시각 미사용 대안) |
| `evaluate_rankings(...)` | NDCG/MRR/HitRate/Precision/Coverage + Cold/Warm/Hot 세그먼트 |
| `ndcg_at_k` / `reciprocal_rank` / `hit` | 순수 지표 함수 |

### 3.2 집계 규칙 (누수 방지)

행렬 셀은 (유저×VOD) 집계값이라 "최신 상호작용 1건만 제거" 하면 **같은 VOD의
과거 시청이 학습셋에 남아 정답이 자명해지는 누수**가 생긴다. 이를 막기 위해:

- 동일 `(유저, VOD)` 반복 시청 → **최신 strt_dt · 최대 confidence 로 1개 셀에 집계**
- 홀드아웃 = 유저의 집계 아이템 중 **최신 시각** 1건 (학습셋에서 완전 제거)
- 최신 시각 동률 → **item_idx 큰 쪽**을 결정적으로 선택 (재현성)
- 서로 다른 아이템 `< 2` 인 유저는 평가 대상에서 제외

### 3.3 데이터 로더 확장

`src/data_loader.py` 에 `load_interactions(conn, filter_quality)` 추가 — 집계 전
`(user, vod, completion_rate, strt_dt)` raw 행 반환. 행렬 로그에 상호작용 건수 추가.

### 3.4 스크립트 인터페이스

`scripts/evaluate.py` 에 `--split {temporal,random,legacy}` 추가.

```bash
python scripts/evaluate.py --split temporal --k 20                 # 기본·권장
python scripts/evaluate.py --split random --seed 42 --filter-quality
python scripts/evaluate.py --split legacy   # 기존 nz[-1] 버그 재현(비교 전용)
```

`legacy` 는 **개선 전/후 비교표**(P0-3)를 위해 의도적으로 보존했고, docstring에
"정확하지 않음" 을 명시했다.

## 4. 검증 (테스트)

`CF_Engine/tests/test_evaluation.py` 신규 13케이스 — 이 세션 환경(Python 3.14,
implicit 미설치)에서 실제 실행해 **전부 통과**.

```
$ python -m pytest CF_Engine/tests/test_evaluation.py -q
13 passed in 1.26s
```

예외 케이스 커버:
- 최신 시각 아이템을 홀드아웃으로 선택 (컬럼 인덱스 최댓값 ≠ 정답) ✔
- 최소 이력(1개) 유저 제외 ✔
- 동일 VOD 반복 시청 누수 없음 ✔
- 최신 시각 동률 결정적 처리 ✔
- confidence 최대값 집계 ✔
- random 분리 재현성(동일 시드=동일 결과) ✔
- NDCG/MRR/HitRate/Coverage/세그먼트 수치 ✔

기존 테스트 회귀 없음: `test_data_loader`, `test_recommender` 12 passed.

## 5. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 분리 규칙이 코드·문서에 명시 | ✅ `evaluation.py` docstring + 본 보고서 |
| 동일 입력·설정으로 재실행 가능 | ✅ temporal 결정적, random 시드 고정 |

## 6. 한계 / 다음 블록 연결

- **실측 지표 미반영**: DB 접속 불가로 temporal 분리 기준 NDCG/HitRate 재측정은
  보류. DB 재실행 시 `--split temporal` 로 산출 예정. 기존 보고 수치
  (NDCG@10 0.2659 등)는 `legacy` 분리 기준임을 명시해야 한다.
- `full_eval.py` 의 temporal 적용 및 **동일 코호트 공정 비교**는 **P0-3** 에서 수행.
- 품질 필터 ON/OFF 설정 일원화는 **P0-2** 에서 수행.

## 변경 파일

- `CF_Engine/src/evaluation.py` (신규)
- `CF_Engine/src/data_loader.py` (`load_interactions` 추가)
- `CF_Engine/scripts/evaluate.py` (`--split` 옵션, evaluation 모듈 연계)
- `CF_Engine/tests/test_evaluation.py` (신규, 13 케이스)
