# CF_Engine — 협업 필터링 추천 엔진

시청 이력(`watch_history`) 기반 ALS 행렬분해 추천. User-Item 잠재 벡터를 학습해
유저별 Top-20 추천을 `serving.vod_recommendation` 에 적재하고, Hybrid_Layer 가
소비한다. 상세 규칙은 [`CLAUDE.md`](CLAUDE.md) 참조.

## 구조

```
src/        import 전용 라이브러리
  base.py            DB 커넥션
  data_loader.py     watch_history → 희소행렬 / 상호작용 로드(strt_dt 포함)
  als_model.py       ALS 학습·추천
  recommender.py     결과 포매팅 + 시리즈 중복제거 + Cold Fallback
  evaluation.py      Hold-out 분리(시간순/무작위) + 랭킹 지표 + 공정 코호트
  runconfig.py       실행 설정(품질 필터) 해석
  db_writer.py       serving.vod_recommendation 적재(DELETE+INSERT / UPSERT)
  monitoring.py      배치 재시도·구조화 로그·지표 변화
scripts/    직접 실행 스크립트
  train.py           학습 + 적재
  run_pipeline.py    정기 배치 래퍼(재시도·로그·알림)   ← 권장 진입점
  evaluate.py        단일 평가 (--split temporal 권장)
  full_eval.py       종합 평가 (--mode fair 권장)
  export_to_db.py    db_writer 재노출(하위호환)
config/als_config.yaml   하이퍼파라미터 + data.filter_quality + recommend.fallback
tests/      pytest
docs/       설계·평가 리포트, AUTOMATION.md, progress/ 보강 중간보고서
```

## 실행

```bash
conda activate myenv

# 학습 + 적재 (정기 배치)
python scripts/run_pipeline.py                 # 재시도·로그 포함
python scripts/train.py --dry-run              # DB 저장 없이 추천만

# 평가
python scripts/evaluate.py --split temporal --k 20
python scripts/full_eval.py --mode fair --k 10

# 테스트
python -m pytest CF_Engine/tests/ -q
```

## 설정 (`config/als_config.yaml`)

| 키 | 의미 |
|---|---|
| `model.*` | factors / iterations / regularization / alpha |
| `data.filter_quality` | poster_url+vod_embedding 보유 VOD만 사용(학습·평가 공통) |
| `recommend.top_k` | 유저별 추천 수(20) |
| `recommend.recommendation_type` | `COLLABORATIVE` (DB CHECK·Hybrid_Layer 계약) |
| `recommend.fallback` | Cold 유저를 인기 추천으로 보충 |
| `db.batch_size` | 적재 배치 크기 |

CLI `--filter-quality` / `--no-filter-quality` 로 실행 단위 override.

## 자동화·모니터링

정기 배치 실행 절차·로그 스키마·실패 대응은 [`docs/AUTOMATION.md`](docs/AUTOMATION.md).
실행 로그는 `logs/run_log.jsonl`(JSONL, Git 미추적).

## 포트폴리오 보강 기록

`CF_Engine_11.md` 작업계획에 따른 보강 내역은 [`docs/progress/`](docs/progress/)
(P0-1 ~ P2 블록별 중간보고서).
