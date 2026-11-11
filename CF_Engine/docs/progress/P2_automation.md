# 중간보고서 — P2 실행 자동화·모니터링

> 대상: `CF_Engine/scripts/run_pipeline.py`, `CF_Engine/src/monitoring.py`, docs/README
> 작업계획: `CF_Engine_11.md` §2 P2
> 작성일: 2026-11-12 · 상태: **코드·테스트·문서 완료**

## 1. 문제 (Before)

실행 코드와 로그는 있었으나, 정기 배치로서의 성숙도(실행 절차 표준화, 성공/실패·
처리량 추적, 실패 재처리·알림)가 없었다. "담당자가 수동으로 모든 단계를 수행하지
않아도 결과를 확인하고 문제를 추적" 하는 수단이 부재.

## 2. 변경 (After)

### 2.1 모니터링 라이브러리 — `src/monitoring.py` (신규, 순수 로직)

| 함수 | 역할 |
|---|---|
| `run_with_retry(fn, retries, backoff, sleep)` | 일시적 실패 지수적 backoff 재시도(총 시도 = retries+1), sleep 주입으로 테스트 가능 |
| `build_run_record(...)` | 실행 1회 레코드(status/started/ended/elapsed/stats/metrics/error) |
| `append_run_log` / `read_run_log` | JSONL append / 읽기 |
| `metrics_delta(prev, curr)` | **직전 성공 대비 처리량 변화량** |
| `last_success(records, job)` | 최근 성공 레코드 조회 |

### 2.2 배치 래퍼 — `scripts/run_pipeline.py` (신규, 권장 진입점)

```
직전 성공 레코드 조회 → run_with_retry( run_pipeline + export ) →
   성공: success 레코드(설정+처리건수+지표변화) append → logs/run_log.jsonl
   실패: failed 레코드 append + send_alert() + exit(1)
```

- **처리 건수·소요 시간·지표 변화**를 구조화 로그(JSONL)로 기록(계획서 체크 2).
- **실패 알림**: `send_alert()` 는 `CF_ALERT_WEBHOOK` 환경변수가 있을 때만 전송,
  없으면 로그만. URL·자격증명 하드코딩 금지(루트 CLAUDE.md 보안규칙 준수).
- **안전 재처리**: 적재는 멱등(P1-2) → 실패 후 그대로 재실행하면 복구(계획서 체크 3).

### 2.3 문서화 (계획서 체크 1·4)

- `docs/AUTOMATION.md`: 실행 스크립트 표, 로그 스키마, 실패 대응 절차,
  cron / Cloud Run Jobs / Windows 작업 스케줄러 **등록 예시**, 운영 체크리스트.
- `README.md`(신규): 구조·실행·설정·자동화 요약.
- `.gitignore`: `CF_Engine/logs/` 추가(실행 로그 미커밋).

## 3. 검증 (테스트)

`CF_Engine/tests/test_monitoring.py` 신규 9케이스 — 이 세션에서 실행해 **통과**.

```
$ python -m pytest CF_Engine/tests/test_monitoring.py -q
9 passed in 0.07s
```

- N회 실패 후 성공 복구 / 재시도 소진 시 예외 + on_error 콜백 횟수 ✔
- 성공 시 대기(sleep) 없음 ✔
- 레코드 elapsed 계산 / JSONL append·read 왕복 / 결측 로그 빈 리스트 ✔
- 지표 변화량 / prev 없을 때 None / 최근 성공 선택 ✔

## 4. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| 정기 배치 실행 절차 문서화 | ✅ `AUTOMATION.md` + README |
| 성공 여부·건수·시간·성능 변화 로그 | ✅ JSONL run record + `metrics_delta` |
| 실패 알림·안전 재처리 | ✅ `send_alert` + 멱등 재실행 + `run_with_retry` |
| 실행 스크립트·설정 README 기재 | ✅ README + AUTOMATION |

## 5. 한계

- `send_alert` 의 실제 웹훅 연동은 환경변수·외부 서비스가 있어야 동작(코드 제공, 미검증).
- 모델 성능 변화(NDCG 등)의 자동 추적은 현재 처리량 지표 중심. 정기 평가
  (`full_eval.py --mode fair`) 결과를 run record `metrics` 에 합류시키면 품질 회귀도
  추적 가능(후속 연결 지점).

## 변경 파일

- `CF_Engine/src/monitoring.py` (신규)
- `CF_Engine/scripts/run_pipeline.py` (신규)
- `CF_Engine/docs/AUTOMATION.md` (신규)
- `CF_Engine/README.md` (신규)
- `CF_Engine/tests/test_monitoring.py` (신규, 9 케이스)
- `.gitignore` (`CF_Engine/logs/` 추가)
