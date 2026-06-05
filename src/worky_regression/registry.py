"""任務單元 registry — 從 cases/_specs/endpoints.yaml 載入單一真實來源。

對外提供：
- ``SPEC``              載入後的完整 dict（分解器用它當「菜單」與 cached context）
- ``TRANSITIONS``       {name: Transition}，runner 依此執行
- ``PUSH_TYPE_IDS``     {type 常量名: type_id}（contract + job 合併）
- ``get(name)``         查單一 transition
- ``unit_spec(name)``   查單一單元的完整規格（含 preconditions / side_effects）

取代舊的 transitions.py / job_transitions.py / push_type_ids.py / job_push_type_ids.py
（那些已改為從本模組 re-export）。加 transition / 改 enum 只需動 endpoints.yaml。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .transitions import Transition

SPEC_PATH = Path(__file__).resolve().parents[2] / "cases" / "_specs" / "endpoints.yaml"


@lru_cache(maxsize=1)
def load_spec(path: Path | None = None) -> dict[str, Any]:
    """讀 endpoints.yaml（快取一次）。"""
    with (path or SPEC_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_push_map(spec: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for group in spec.get("push_types", {}).values():
        out.update(group)
    return out


def _build_transitions(spec: dict[str, Any]) -> dict[str, Transition]:
    out: dict[str, Transition] = {}
    for name, u in spec.get("task_units", {}).items():
        push = u.get("push") or {}
        out[name] = Transition(
            name=name,
            actor_role=u["actor"],
            method=u.get("method", "POST"),
            endpoint=u["endpoint"],
            fires_event=u.get("event", "") or "",
            pushes_to=push.get("to", "") or "",
            push_type_id=push.get("type", "") or "",
            doc_id=str(u.get("doc_id", "")),
            body_template=u.get("request") or {},
            api_group=u.get("api", "main"),
        )
    return out


SPEC: dict[str, Any] = load_spec()
TRANSITIONS: dict[str, Transition] = _build_transitions(SPEC)
PUSH_TYPE_IDS: dict[str, int] = _build_push_map(SPEC)


def get(name: str) -> Transition:
    if name not in TRANSITIONS:
        raise KeyError(f"unknown transition: {name}. available: {sorted(TRANSITIONS)}")
    return TRANSITIONS[name]


def unit_spec(name: str) -> dict[str, Any]:
    """回傳單元在 YAML 內的完整規格（含 preconditions / side_effects）。"""
    units = SPEC.get("task_units", {})
    if name not in units:
        raise KeyError(f"unknown task unit: {name}")
    return units[name]
