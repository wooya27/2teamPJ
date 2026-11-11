"""
STEP 6: 추천 결과 → serving.vod_recommendation 적재 (얇은 래퍼)

실제 로직은 재사용·테스트 가능한 src/db_writer.py 에 있다.
train.py 의 `from export_to_db import export` 하위호환을 위해 재노출한다.

실행: train.py 내부에서 호출.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db_writer import export, export_upsert  # noqa: F401  (재노출)

__all__ = ["export", "export_upsert"]
