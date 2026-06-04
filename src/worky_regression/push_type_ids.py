"""向後相容 shim — push type 全表已移至 cases/_specs/endpoints.yaml。

請改用 ``registry.PUSH_TYPE_IDS``（contract + job 合併）。本檔僅保留承攬制段。
"""
from __future__ import annotations

from .registry import SPEC

PUSH_TYPE_IDS: dict[str, int] = dict(SPEC["push_types"]["contract"])
