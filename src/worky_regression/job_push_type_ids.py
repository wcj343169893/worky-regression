"""向後相容 shim — 工作系統 push type 已移至 cases/_specs/endpoints.yaml。

請改用 ``registry.PUSH_TYPE_IDS``。本檔僅保留 job 段。
"""
from __future__ import annotations

from .registry import SPEC

JOB_PUSH_TYPE_IDS: dict[str, int] = dict(SPEC["push_types"]["job"])
