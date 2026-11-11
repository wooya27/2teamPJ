# CF_Engine 포트폴리오 보강 — 블록별 중간보고서

`CF_Engine_11.md` 작업계획의 P0~P2 블록을 순서대로 보강한 기록.
각 보고서는 문제(Before) → 변경(After) → 검증(테스트) → 완료기준 대비 → 한계 구조.

| 블록 | 보고서 | 핵심 변경 | 테스트 |
|---|---|---|---|
| **P0-1** 평가 분리 | [P0-1_eval_split.md](P0-1_eval_split.md) | `nz[-1]` 버그 → `strt_dt` 시간순 Hold-out, `src/evaluation.py` 추출 | 13 |
| **P0-2** 필터 통일 | [P0-2_filter_config.md](P0-2_filter_config.md) | `config.data.filter_quality` + CLI override, 학습에 필터 전달 | 9 |
| **P0-3** 공정 비교 | [P0-3_fair_compare.md](P0-3_fair_compare.md) | 동일 코호트 `align_fair_cohort`, fair/legacy 모드 | 4 |
| **P1-1** Cold Fallback | [P1-1_cold_fallback.md](P1-1_cold_fallback.md) | 인기 추천 읽어 Top-K 보충, 중복/시리즈/기시청 제외 | 9 |
| **P1-2** DB 적재 안정성 | [P1-2_db_stability.md](P1-2_db_stability.md) | `src/db_writer.py`, 트랜잭션 롤백·멱등, UPSERT 대안 | 10 |
| **P1-3** 통합 테스트 | [P1-3_integration.md](P1-3_integration.md) | 로드→추천→적재 통합, `COLLABORATIVE` 계약 확인 | 4 |
| **P2** 자동화·모니터링 | [P2_automation.md](P2_automation.md) | `run_pipeline.py` + `src/monitoring.py`, 재시도·JSONL 로그·알림 | 9 |

**합계 신규 테스트 58케이스**(전체 `pytest CF_Engine/tests/`: 70 passed, 1 skipped).

## 공통 전제

- **DB 접속 불가** 환경이라 코드·단위테스트까지 완료, **실측 지표는 DB 재실행 시** 산출.
- 기존 보고 수치(NDCG@10 0.2659 등)는 `legacy` 분리·집합 상이 비교이므로 포트폴리오에는
  "동일 조건 재측정 전, 직접 비교 불가"로 표기 필요(P0-1·P0-3 참조).
- 새 테스트는 implicit·DB 의존 없이 순수 numpy/scipy 로 작성되어 CI에서 그대로 실행 가능.

## 재실행(지표 재측정) 명령

```bash
conda activate myenv
python scripts/evaluate.py --split temporal --k 20          # 시간순 평가
python scripts/full_eval.py --mode fair --k 10              # 동일 코호트 공정 비교
python scripts/run_pipeline.py                              # 학습+적재(자동화)
python -m pytest CF_Engine/tests/ -q                        # 테스트
```
