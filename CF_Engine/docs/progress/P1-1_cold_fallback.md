# 중간보고서 — P1-1 Cold 사용자 Fallback 및 추천 부족 대응

> 대상: `CF_Engine/src/recommender.py`, `CF_Engine/scripts/train.py`
> 작업계획: `CF_Engine_11.md` §2 P1-1
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료**

## 1. 문제 정의 (계획서 체크 1)

ALS 는 협업 신호가 적은 유저(cold)에서 추천이 부족하다. 구체적 부족 상황:

- **추천 0건**: 학습 상호작용이 희박해 모델이 후보를 못 내는 유저.
- **Top-K 미달**: 시리즈 중복 제거(`build_records`) 후 20개가 안 되는 유저.

기존 파이프라인은 이런 유저를 **그대로 부족한 채** 적재했다(보충 정책 없음).

## 2. 기존 자원 연결 확인 (계획서 체크 2)

프로젝트에 이미 **`serving.popular_recommendation`** 테이블이 존재하고
`Normal_Recommendation` 브랜치가 CT_CL별 인기 추천('POPULAR')을 적재한다
(`Database_Design/schemas/create_popular_recommendation.sql`).
→ CF는 이를 **새로 만들지 않고 '읽기' 로 재사용**해 Fallback 후보로 쓴다
(브랜치 간 책임 분리 유지, 루트 CLAUDE.md Rule 1·3 준수).

## 3. 변경 (After)

### 3.1 `recommender.py` — 순수 함수 2종 추가

| 함수 | 역할 |
|---|---|
| `load_popular_items(conn, limit)` | `serving.popular_recommendation` 에서 score 내림차순 인기 VOD 로드(중복 vod는 MAX score) |
| `apply_fallback(records, popular_items, top_k, ...)` | Top-K 미달 유저를 인기 아이템으로 **중복 없이** 채움 |

**`apply_fallback` 제외 규칙** (Top-K 를 넘지 않게 채움):
- 이미 그 유저에게 추천된 VOD
- 이미 포함된 **시리즈**(단, `TV 연예/오락` 은 에피소드 단위 유지 → 중복 허용)
- 그 유저가 **이미 시청한 VOD**(`watched_by_user` 제공 시)
- 추천 **0건 cold 유저**도 `all_user_ids` 로 넘기면 인기 추천으로 신규 생성

반환 `stats`: `users_underfilled_before / users_zero_before / users_filled /
items_added / users_still_short_after` → **Fallback 사용률·충족률 수집**(계획서 체크 5).

### 3.2 파이프라인 연계 — `train.py`

- config `recommend.fallback: true`, `fallback_limit: 200` 추가.
- `run_pipeline` 가 인기 아이템 로드 → `build_records` 후 `apply_fallback` 적용 → 통계 로그.
- **메모리 절약**: 기시청 집합(`watched_by_user`)을 전체 유저가 아니라
  `_watched_for_underfilled()` 로 **보충이 필요한(top_k 미만) 유저만** 계산.

## 4. 검증 (테스트)

`CF_Engine/tests/test_fallback.py` 신규 9케이스 — 이 세션에서 실행해 **통과**.

```
$ python -m pytest CF_Engine/tests/test_fallback.py -q
9 passed in 0.05s
```

계획서 체크 4의 대표 시나리오 전부 커버:
- 추천 0건 cold 유저 → 인기로 Top-K 생성 ✔
- Top-K 미달 유저 보충(랭크 연속성) ✔
- 기존 추천·기시청 VOD 중복 제외 ✔
- 시리즈 중복 많은 유저: 같은 시리즈 1건만 / `TV 연예/오락` 은 에피소드 유지 ✔
- 인기 후보 부족 시 잔여 부족(`users_still_short_after`) 보고 ✔
- 이미 꽉 찬 유저 무변경 ✔

## 5. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 부족 상황 정의 | ✅ 0건 / Top-K 미달 |
| 기존 대체 추천 연결 | ✅ `serving.popular_recommendation` 읽기 |
| Top-K 미달 시 중복 없이 채움 | ✅ `apply_fallback` |
| 0건/중복 시리즈/필터 콘텐츠 적은 유저 테스트 | ✅ 9 케이스 |
| Fallback 사용률·충족률 수집 | ✅ `stats` + 로그 |

## 6. 한계 / 다음 블록

- 인기 Fallback 항목의 `score` 는 ALS 점수와 구분되도록 **0.0 센티넬**로 저장하고
  `recommendation_type` 은 DB CHECK 제약(`COLLABORATIVE`)을 유지한다. Hybrid_Layer가
  CF 출처를 더 세분화해야 하면 별도 플래그 컬럼 신설을 Database_Design에 제안 필요.
- 실제 Fallback 사용률 수치는 DB 재실행 로그에서 확보.
- 다음: **P1-2 DB 적재 안정성/장애 복구**.

## 변경 파일

- `CF_Engine/src/recommender.py` (`load_popular_items`, `apply_fallback` 추가)
- `CF_Engine/scripts/train.py` (fallback 연계 + `_watched_for_underfilled`)
- `CF_Engine/config/als_config.yaml` (`recommend.fallback`, `fallback_limit`)
- `CF_Engine/tests/test_fallback.py` (신규, 9 케이스)
