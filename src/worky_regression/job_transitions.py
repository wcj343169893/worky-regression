"""向後相容 shim — 工作系統任務單元已移至 cases/_specs/endpoints.yaml。

請改用 ``registry``。本檔僅為舊匯入路徑保留 ``JOB_TRANSITIONS`` / ``get``。
"""
from __future__ import annotations

from .registry import SPEC, TRANSITIONS
from .transitions import Transition

JOB_TRANSITIONS: dict[str, Transition] = {
    name: t for name, t in TRANSITIONS.items()
    if SPEC["task_units"][name].get("system") == "job"
}


def get(name: str) -> Transition:
    if name not in JOB_TRANSITIONS:
        raise KeyError(f"unknown job transition: {name}. available: {sorted(JOB_TRANSITIONS)}")
    return JOB_TRANSITIONS[name]
