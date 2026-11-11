# 중간보고서 — P0-2 학습·평가 품질 필터 설정 통일

> 대상: `CF_Engine/scripts/train.py`, `CF_Engine/src/data_loader.py`, `CF_Engine/config/als_config.yaml`
> 작업계획: `CF_Engine_11.md` §2 P0-2
> 작성일: 2026-11-12 · 상태: **코드·테스트 완료**

## 1. 문제 (Before)

- 평가(`evaluate.py`)에는 `--filter-quality` 옵션이 있으나 **학습(`train.py`)은
  `load_matrix(conn, alpha=...)` 만 호출**해 필터 옵션을 전달하지 않았다.
  → 학습은 항상 전체 데이터, 평가는 필터 ON/OFF 가 섞여 **학습셋과 평가셋 조건이
  어긋남**.
- `config/als_config.yaml` 에 필터 설정 항목 자체가 없어, 어떤 조건으로 모델이
  만들어졌는지 설정만 보고 알 수 없었다(재현성 결함).

## 2. 변경 (After)

### 2.1 설정 단일화 — `config/als_config.yaml`

```yaml
data:
  # True → poster_url AND vod_embedding 둘 다 있는 VOD만 학습·평가에 사용.
  # 학습과 평가가 동일한 필터 정의를 공유. CLI 로 실행 단위 override 가능.
  filter_quality: false
```

### 2.2 우선순위 해석기 — `src/runconfig.py` (신규, 순수 함수)

`resolve_filter_quality(config, cli_filter, cli_no_filter)`

| 우선순위 | 소스 |
|---|---|
| 1 | CLI `--filter-quality` / `--no-filter-quality` |
| 2 | YAML `data.filter_quality` |
| 3 | 기본 `False` |

두 CLI 플래그 동시 지정 시 `ValueError`. `describe_run()` 은 확정된 설정을
딕셔너리로 반환(로그·`--dry-run` 표기용).

### 2.3 학습 파이프라인 — `scripts/train.py`

- `run_pipeline(cfg, filter_quality)` 로 변경해 `load_matrix(..., filter_quality=...)` 전달.
- 학습 직후 **유저 수 / 아이템 수 / 상호작용 수 / 필터 ON·OFF 로그** 출력.
- `--dry-run` 포함 **모든 실행에서 확정 설정을 로그로 먼저 출력** →
  "어떤 조건으로 돌렸는지" 를 실행 로그만으로 확인 가능.

```bash
python scripts/train.py --dry-run                 # config 값 사용, 저장 생략
python scripts/train.py --filter-quality          # 이번 실행만 필터 ON
python scripts/train.py --no-filter-quality       # 이번 실행만 필터 OFF
```

### 2.4 부수 수정 (방어)

빈 결과 로드 시 `sparsity = 1 - nnz/(rows*cols)` 가 `ZeroDivisionError` 를 내던
문제를 가드 처리(`cells else 0.0`). 테스트 중 발견.

## 3. 검증 (테스트)

`CF_Engine/tests/test_runconfig.py` 신규 9케이스 — 이 세션에서 실행해 **통과**.

```
$ python -m pytest CF_Engine/tests/test_runconfig.py CF_Engine/tests/test_data_loader.py -q
15 passed in 0.39s
```

- 설정 우선순위(기본/YAML/CLI override/충돌 예외) ✔
- **필터 SQL 구조 검증**: `filter_quality=True` 쿼리에 `poster_url` **AND**
  `vod_embedding` **AND** `is_test = FALSE` 가 모두 포함, OFF 쿼리엔 `vod_embedding`
  조인 없음 ✔ (계획서 "poster_url과 vod_embedding을 모두 요구하는지 테스트" 충족)
- `load_interactions` 필터 쿼리에 `strt_dt` 포함(P0-1 시간순 분리 연계) ✔

## 4. 완료 기준 대비

| 계획서 완료 기준 | 상태 |
|---|---|
| filter_quality 를 YAML/CLI 로 관리 | ✅ `data.filter_quality` + CLI override |
| 학습·평가가 동일 필터 정의 사용 | ✅ 동일 `load_matrix(filter_quality=...)` 경로 |
| 필터 실행 시 유저/콘텐츠/상호작용 수 로그 | ✅ `run_pipeline` 로그 |
| `--dry-run` 에서 실제 선택 설정 확인 | ✅ `describe_run` 로그 |
| poster_url+vod_embedding 둘 다 요구 테스트 | ✅ SQL 구조 테스트 |

## 5. 한계 / 다음 블록

- 평가 스크립트(`evaluate.py`)는 이미 `--filter-quality` 를 받으므로, 학습·평가가
  같은 플래그로 정렬됨. 다만 **ON/OFF 평가 집합이 여전히 서로 다름**(필터가
  유저·아이템 집합을 바꿈) → 동일 코호트 공정 비교는 **P0-3** 에서 처리.

## 변경 파일

- `CF_Engine/config/als_config.yaml` (`data.filter_quality` 추가)
- `CF_Engine/src/runconfig.py` (신규)
- `CF_Engine/scripts/train.py` (필터 전달·로그·dry-run 설정 출력)
- `CF_Engine/src/data_loader.py` (빈 행렬 방어)
- `CF_Engine/tests/test_runconfig.py` (신규, 9 케이스)
