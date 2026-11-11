# CF_Engine 실행 자동화·모니터링

정기 학습·적재 배치의 실행 절차, 로그 스키마, 실패 대응을 정의한다.

## 1. 실행 스크립트

| 스크립트 | 용도 |
|---|---|
| `scripts/train.py` | 학습 + 적재 1회 실행(수동) |
| `scripts/run_pipeline.py` | **정기 배치 래퍼**: 재시도 + 구조화 로그 + 실패 알림 |
| `scripts/evaluate.py` | 품질 평가(단일) — `--split temporal` 권장 |
| `scripts/full_eval.py` | 종합 평가 — `--mode fair` 권장 |

```bash
# 정기 배치(권장 진입점)
python scripts/run_pipeline.py                 # 학습 + 적재
python scripts/run_pipeline.py --dry-run       # DB 저장 없이 검증
python scripts/run_pipeline.py --retries 2 --backoff 10
```

## 2. 실행 로그 (모니터링)

`run_pipeline.py` 는 매 실행 결과를 **`logs/run_log.jsonl`** 에 1줄(JSON)씩 append 한다.

```json
{"job":"cf_train","status":"success","started_at":"...","ended_at":"...",
 "elapsed_sec":123.4,
 "stats":{"filter_quality":false,"users":166766,"items":...,"records":...,"inserted":...},
 "metrics":{"records":20,"inserted":18},   // 직전 성공 대비 변화량
 "error":null}
```

- `status`: `success` | `failed`
- `stats`: 확정 설정 + 처리 건수(유저/아이템/레코드/적재)
- `metrics`: **직전 성공 실행 대비 변화량**(처리량 급변 감지)
- `elapsed_sec`: 처리 시간

로그 조회 예:

```bash
tail -n 5 logs/run_log.jsonl
python -c "from CF_Engine.src.monitoring import read_run_log, last_success; \
recs=read_run_log('CF_Engine/logs/run_log.jsonl'); print(last_success(recs,'cf_train'))"
```

## 3. 실패 대응 (안전 재처리)

1. **재시도**: `run_with_retry(retries, backoff)` 가 일시적 실패(DB 순단 등)를
   지수적 backoff 로 재시도.
2. **원자적 적재**: 적재 실패 시 `db_writer.export` 가 rollback → **부분 적재 없음**.
   재실행은 멱등(해당 type·user 삭제 후 재삽입)이라 **그대로 다시 돌리면 복구**.
3. **알림**: 모든 재시도 실패 시 `failed` 레코드 기록 + `send_alert()` 호출 +
   종료코드 1. 웹훅은 환경변수 `CF_ALERT_WEBHOOK` 로만 주입(하드코딩 금지).

## 4. 스케줄 등록 예시

**cron (Linux)**

```cron
# 매일 03:00 KST 학습·적재
0 3 * * *  cd /app/CF_Engine && /opt/conda/envs/myenv/bin/python scripts/run_pipeline.py >> logs/cron.out 2>&1
```

**Cloud Run Jobs**: 컨테이너 ENTRYPOINT 를 `python scripts/run_pipeline.py` 로 두고
Cloud Scheduler 로 트리거. DB 자격증명은 Secret Manager → 환경변수 주입
(루트 CLAUDE.md 보안규칙: `.env` 미커밋, 기본값에 인프라 정보 금지).

**Windows 작업 스케줄러**

```powershell
schtasks /Create /TN "CF_Engine" /TR "conda run -n myenv python scripts\run_pipeline.py" /SC DAILY /ST 03:00
```

## 5. 운영 체크리스트

- [ ] `logs/run_log.jsonl` 최신 레코드 `status == success`
- [ ] `inserted` 건수가 직전 대비 비정상 급감하지 않음(`metrics`)
- [ ] 실패 시 알림 수신 → 원인 확인 후 `run_pipeline.py` 재실행(멱등)
