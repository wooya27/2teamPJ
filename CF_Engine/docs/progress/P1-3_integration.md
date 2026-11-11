# 중간보고서 — P1-3 통합 테스트 및 API 연계 검증

> 대상: `CF_Engine/tests/`, `Hybrid_Layer/`, `API_Server/`
> 작업계획: `CF_Engine_11.md` §2 P1-3
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료 (서비스 기동은 DB/런타임 필요)**

## 1. 목표

데이터 로드 → 추천 생성 → 결과 포매팅 → Fallback → DB 적재 **전 구간**을 하나의
테스트로 연결하고, CF 출력이 후속 모듈(Hybrid_Layer·API_Server)과 **계약상 일치**함을
증명한다.

## 2. 통합 테스트 — `tests/test_integration.py` (신규 4케이스)

implicit·실DB 없이 전 구간을 연결한다. ALS 학습만 implicit 의존이므로 **'ALS 추천
출력'(item_indices/scores)만 주입**하고 그 이후 전 구간을 실제 코드로 실행한다.

```
DataLoader.load_matrix(watch_history stub) → 인코더·행렬
   → [ALS 추천 출력 주입]
   → build_records (시리즈 중복 제거, 랭크)       ← 실제 코드
   → apply_fallback (부족 유저 보충)               ← 실제 코드
   → db_writer.export (FakeConn + execute_batch patch)  ← 실제 코드
```

### 검증 내용 (계획서 체크 1·3·5)

| 테스트 | 검증 |
|---|---|
| `test_full_pipeline_data_to_db` | 전 구간 연결 + 시리즈 중복 제거(A2 제거) + **이미 시청한 vodX 제외** + 모든 레코드 `COLLABORATIVE` + DELETE→INSERT 순서 + commit 1회 |
| `test_top_k_cap_enforced` | 서로 다른 시리즈 30개 → **Top-20 상한** 확보 |
| `test_underfilled_user_filled_then_loaded` | 추천 부족 유저 Fallback 보충 후 DB 적재까지 |
| `test_db_failure_propagates` | **DB(외부 모듈) 실패 시 rollback + 예외 전파 + 부분 적재 없음** |

```
$ python -m pytest CF_Engine/tests/test_integration.py -q
4 passed in 0.35s
```

## 3. API 연계 계약 검증 (계획서 체크 2·4)

실제 서비스 기동 없이, **`recommendation_type='COLLABORATIVE'` 값이 후속 모듈과
일치**함을 코드 교차 확인으로 증명:

| 소비처 | 근거 |
|---|---|
| Hybrid_Layer 리랭커 | `Hybrid_Layer/src/reranker.py:267` `cf_items = [s for s in scored if "COLLABORATIVE" in s["source_engines"]]` |
| Hybrid 평가 | `Hybrid_Layer/scripts/evaluate_hybrid_vs.py:246` `load_recommendations(conn, "COLLABORATIVE", ...)` |
| DB CHECK 제약 | `create_embedding_tables.sql:295` `CHECK (recommendation_type IN ('VISUAL_SIMILARITY','COLLABORATIVE','HYBRID','CONTENT_BASED'))` |
| CF 설정 | `CF_Engine/config/als_config.yaml:17` `recommendation_type: "COLLABORATIVE"` |

→ CF 가 적재하는 타입을 Hybrid_Layer 가 그대로 읽고, DB 제약도 허용. **계약 일치.**

## 4. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 로드→생성→포매팅→적재 통합 테스트 | ✅ 4케이스 |
| `recommendation_type='COLLABORATIVE'` 후속 일치 | ✅ 교차 확인 |
| 시리즈 중복 제거·기시청 제외·Top-20 확보 | ✅ 테스트 |
| Hybrid/API 실제 추천 확인 | ⚠ 계약(코드/스키마) 수준 확인. **실응답 확인은 DB+서비스 기동 필요** |
| DB/외부 모듈 실패 시 동작 | ✅ rollback + 전파 테스트 |

## 5. 한계 / 다음 블록

- **실 서비스 응답 캡처**(Hybrid_Layer 리랭킹 결과, API_Server JSON)는 DB 접속·서비스
  기동이 필요해 이번 세션 범위 밖. DB 가용 시 `scripts/train.py` 적재 →
  Hybrid_Layer → `API_Server` 호출로 end-to-end 캡처를 포트폴리오 04/05페이지에 첨부.
- 다음: **P2 실행 자동화·모니터링**.

## 변경 파일

- `CF_Engine/tests/test_integration.py` (신규, 4 케이스)
