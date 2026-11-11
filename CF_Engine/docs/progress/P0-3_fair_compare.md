# 중간보고서 — P0-3 공정한 추천 성능 비교

> 대상: `CF_Engine/scripts/full_eval.py`, `CF_Engine/src/evaluation.py`, 평가 리포트
> 작업계획: `CF_Engine_11.md` §2 P0-3
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료 / 지표 재측정은 DB 재실행 시**

## 1. 문제 (Before)

기존 `full_eval.py` 는 필터 OFF/ON 을 **각각 별도 행렬로 로드**해 비교했다.

```python
for label, use_filter in [("필터 없음", False), ("필터 적용", True)]:
    mat, ... = load_matrix(conn, filter_quality=use_filter)   # 집합이 매번 달라짐
    train_mat, test_items = split_holdout(mat)                # 평가 유저도 달라짐
```

ON/OFF 는 **평가 사용자 수도(147,104 vs 166,766), 아이템 집합도, 정답도 모두 다르다.**
그 상태로 `NDCG 0.2659 vs 0.2353` 를 "향상" 이라 쓰면, 모델이 좋아진 것인지
단지 "쉬운 유저만 남은 것" 인지 구분할 수 없다. 계획서 §0 의 주의사항과 동일한 함정.

## 2. 비교 목적의 명시적 구분 (계획서 체크 1)

| 질문 | 설계 |
|---|---|
| (A) **모델 자체 성능** 비교 | 동일 사용자·동일 정답 고정, 후보 풀만 변경 → `fair` 모드 |
| (B) **추천 가능 콘텐츠 품질** 비교 | 필터로 집합이 바뀌는 것 자체가 결과 → `legacy` 모드(대조용) |

본 작업은 (A) 를 신규 구현하고, (B) 는 함정을 드러내는 경고와 함께 보존한다.

## 3. 변경 (After) — `fair` 모드

### 3.1 동일 코호트 정렬 — `src/evaluation.align_fair_cohort()` (순수 함수)

```
전체 상호작용 → temporal 분리 1회 → 유저별 '고정' 홀드아웃·학습 이력 확정
   ├─ OFF 학습행렬 = 전체 train_mat
   └─ ON  학습행렬 = train_mat 에서 '품질 아이템 컬럼만' 유지 (동일 shape 유지)
공정 평가 유저 = 홀드아웃 정답이 품질 아이템인 유저 (두 모델 모두 맞힐 수 있는 집합)
   └─ 정답이 필터로 사라진 유저는 제외하고 그 수를 보고 (계획서 체크 3)
```

핵심: **인코더 공간(shape)을 공유**하므로 OFF/ON 모델의 아이템 인덱스가 호환되고,
`test_items_fair`·`history_by_user` 가 두 모델에 **동일하게** 적용된다 → apples-to-apples.

**추천 후보 제한(중요)**: 학습행렬 컬럼을 0으로 만들어도 ALS는 비품질 아이템을
낮은 점수로 추천 후보에 포함할 수 있다. 따라서 ON 평가 시 `model.recommend(..., items=quality_cols)`
로 **추천 후보 자체를 품질 아이템으로 제한**해, "품질 콘텐츠만 추천" 조건을 엄밀히 보장한다
(`align_fair_cohort` 가 `quality_cols` 반환). Coverage 분모도 품질 아이템 수로 맞춘다.

### 3.2 리포트 산출물 (계획서 체크 4·5·6)

`full_eval.py --mode fair` 가 리포트에 다음을 함께 기재:

- **NDCG@K / MRR / HitRate@K / Precision@K / Coverage** + 평가 대상 수
- **공정 평가 유저 수**(양쪽 동일)와 **정답이 필터로 제외된 유저 수**
- 전체 아이템 수 / 품질 아이템 수
- **Cold(2~4) / Warm(5~19) / Hot(20+) 세그먼트별** NDCG·MRR·HitRate

### 3.3 대조용 `legacy` 모드

`--mode legacy` 는 구방식(별도 집합)을 재현하되 리포트 상단에
`⚠ OFF/ON 평가 사용자·아이템 집합이 서로 다름 — 직접 비교 불가` 경고를 출력.
면접에서 "왜 단순 비교가 틀렸는지" 를 설명하는 증거로 사용.

## 4. 검증 (테스트)

`CF_Engine/tests/test_fair_compare.py` 신규 4케이스 — 이 세션에서 실행해 **통과**.

```
$ python -m pytest CF_Engine/tests/test_fair_compare.py -q
4 passed in 0.36s
```

- `restrict_matrix_columns`: 비품질 컬럼 질량 0, shape 유지 ✔
- 홀드아웃 정답이 비품질인 유저 **제외**, 품질인 유저 **포함** ✔
- ON 학습행렬엔 비품질 아이템 질량 0 / OFF엔 존재, 동일 shape ✔
- `quality_cols` 반환(ON 추천 후보 제한용) ✔
- stats 의 전체·품질 아이템 수 집계 ✔

## 5. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 비교 목적(모델 vs 콘텐츠) 구분 | ✅ fair/legacy 모드 분리 |
| 동일 평가 사용자·정답·분할 규칙 | ✅ `align_fair_cohort` |
| 필터로 정답 사라진 유저 제외 수 보고 | ✅ `excluded_holdout_filtered` |
| NDCG/HitRate/MRR/Coverage + 대상 수 | ✅ 리포트 |
| Cold/Warm/Hot 성능 비교 | ✅ 세그먼트 표 |
| 차이 원인·한계 서술 | ✅ 리포트 템플릿 + 본 보고서 |

## 6. 한계 / 다음 블록

- **실측 수치 미반영**: DB 재실행 시 `python scripts/full_eval.py --mode fair --k 10`
  으로 산출. 기존 `0.2659 / 0.2353` 은 **집합이 다른 비교**이자 **legacy(무작위) 분리**
  기준이므로, 포트폴리오에는 "동일 조건 재측정 전 수치, 직접 비교 불가" 로 표기해야 함.
- P0 3블록(P0-1·P0-2·P0-3)으로 **평가 신뢰성·설정 재현성·공정 비교**의 코드 토대가
  완성됨. 이후 P1 은 서비스 연계(Fallback·DB 적재·통합 테스트)로 진행.

## 변경 파일

- `CF_Engine/src/evaluation.py` (`restrict_matrix_columns`, `align_fair_cohort` 추가)
- `CF_Engine/scripts/full_eval.py` (fair/legacy 모드 재작성)
- `CF_Engine/tests/test_fair_compare.py` (신규, 4 케이스)
