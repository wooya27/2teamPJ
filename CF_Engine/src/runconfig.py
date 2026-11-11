"""CF_Engine 실행 설정 해석(resolve) 유틸.

학습/평가 파이프라인이 공유하는 런타임 설정을 한곳에서 결정한다.
순수 함수로 구현해 CLI·YAML 조합을 단위 테스트할 수 있게 한다.
"""

from __future__ import annotations


def resolve_filter_quality(config: dict, cli_filter: bool = False,
                           cli_no_filter: bool = False) -> bool:
    """품질 필터(filter_quality) 최종값 결정.

    우선순위: CLI 명시 > YAML(data.filter_quality) > 기본 False.
    `--filter-quality` 와 `--no-filter-quality` 를 동시에 주면 오류.

    Args:
        config: als_config.yaml 를 로드한 dict.
        cli_filter: --filter-quality 전달 여부.
        cli_no_filter: --no-filter-quality 전달 여부.
    """
    if cli_filter and cli_no_filter:
        raise ValueError("--filter-quality 와 --no-filter-quality 를 동시에 지정할 수 없습니다.")
    if cli_filter:
        return True
    if cli_no_filter:
        return False
    return bool(config.get("data", {}).get("filter_quality", False))


def describe_run(config: dict, filter_quality: bool, dry_run: bool) -> dict:
    """실행 직전 확정된 설정 요약(로그·--dry-run 출력용)."""
    m = config.get("model", {})
    r = config.get("recommend", {})
    return {
        "filter_quality": filter_quality,
        "dry_run": dry_run,
        "factors": m.get("factors"),
        "iterations": m.get("iterations"),
        "regularization": m.get("regularization"),
        "alpha": m.get("alpha"),
        "top_k": r.get("top_k"),
        "recommendation_type": r.get("recommendation_type"),
    }
