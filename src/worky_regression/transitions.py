"""Transition dataclass 定義。

任務單元「資料」（10+9 個 transition、endpoint、push 等）已移至單一真實來源
``cases/_specs/endpoints.yaml``，由 ``registry.py`` 載入。本檔僅保留 dataclass；
``TRANSITIONS`` / ``get`` 透過 module ``__getattr__`` 延遲委派給 registry
（向後相容；延遲是為了避開 registry → transitions 的 circular import）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Transition:
    name: str
    """transition 唯一 ID，例如 T2_receiver_apply"""

    actor_role: str
    """觸發角色：publisher / receiver"""

    method: str
    """HTTP method"""

    endpoint: str
    """API path（相對 /v1，例如 /contract/receiver-match-task/task-apply）"""

    fires_event: str
    """對應的 PHP Event 類別 FQCN"""

    pushes_to: str
    """推播給：publisher / receiver / other"""

    push_type_id: str
    """common\\components\\PushNotification\\Type 常量名稱"""

    doc_id: str
    """API 文件編號，例如 502-1"""

    body_template: dict = field(default_factory=dict)
    """request body 模板（runner 會替換 {{...}} 變數）"""


def __getattr__(name: str):
    """向後相容：TRANSITIONS / get 委派給 registry（延遲匯入避開 circular）。"""
    if name in ("TRANSITIONS", "get"):
        from . import registry
        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

