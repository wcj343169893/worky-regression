"""狀態機定義：10 個 PusherOfTask 重構對應的審批流 transition。

每個 transition 對應：
- 一個觸發角色 + 一個 endpoint
- 一個 Event（PHP class）
- 一個推播給對方
- 預期的 DB state 變化

實際 endpoint 路徑會在跑通 smoke test 後逐一補完（先以文檔範例對齊）。
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


# ============================================================
# PusherOfTask 重構觸碰的 10 個 transition
# 對應 commit b887687ee 重構的 10 個 PushNotification 類別
# ============================================================

TRANSITIONS: dict[str, Transition] = {
    # ===== Setup transition（非 PushNotification 重構範圍，但 path 需要） =====
    "T1_publisher_publish_task": Transition(
        name="T1_publisher_publish_task",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/task/publish",
        fires_event="",
        pushes_to="",
        push_type_id="",
        doc_id="402",
        body_template={
            "name": "regression {{state.run_id}}",
            "task_type_level1": 1,                   # 「其他」
            "city_id": 19,                            # 臺北市
            "district_id": 194,                       # 自 DB 既存 contract task 拿出最常用 pair
            "description": "regression auto-task",
            "start_time": "{{state.start_time}}",     # 由 runner 設定 (now + 1d)
            "end_time": "{{state.end_time}}",
            "recruit_count": 1,
            "task_amount": 300,                       # >= 296 才過驗證（系統下限）
            "payment_method_id": 1,                   # FunPoint 信用卡（dev 自動扣款）
            "contact_name": "regression",
            "contact_phone": "0912345678",
            "show_to_taker": 0,
            "photos": [],
            "match_target_favor": 0,
        },
    ),

    # ===== 5 個 receiver 觸發 =====
    "T2_receiver_apply": Transition(
        name="T2_receiver_apply",
        actor_role="receiver",
        method="POST",
        endpoint="/contract/receiver-match-task/task-apply",
        fires_event="common\\components\\Contract\\Event\\AfterReceiverApplyTaskEvent",
        pushes_to="publisher",
        push_type_id="CONTRACT_RECEIVER_APPLY_TASK",
        doc_id="502-1",
        body_template={"task_sn": "{{state.task_sn}}"},
    ),
    "T3_receiver_accept_invite": Transition(
        name="T3_receiver_accept_invite",
        actor_role="receiver",
        method="POST",
        endpoint="/contract/receiver-match-task/task-accept",
        fires_event="common\\components\\Contract\\Event\\AfterAcceptPublisherInviteEvent",
        pushes_to="publisher",
        push_type_id="CONTRACT_RECEIVER_ACCEPT_INVITE",
        doc_id="502-4",
        body_template={"task_sn": "{{state.task_sn}}"},
    ),
    "T5_receiver_cancel_task": Transition(
        name="T5_receiver_cancel_task",
        actor_role="receiver",
        method="POST",
        endpoint="/contract/receiver-task/task-cancel",
        fires_event="common\\components\\Contract\\Event\\AfterReceiverCancelTaskEvent",
        pushes_to="publisher",
        push_type_id="CONTRACT_RECEIVER_CANCEL_TASK",
        doc_id="505",
        body_template={"task_sn": "{{state.task_sn}}", "canceled_reason_id": 1},
    ),
    "T6_receiver_start_task": Transition(
        name="T6_receiver_start_task",
        actor_role="receiver",
        method="POST",
        endpoint="/contract/receiver-task/task-start",
        fires_event="common\\components\\Contract\\Event\\AfterReceiverStartTaskEvent",
        pushes_to="publisher",
        push_type_id="CONTRACT_RECEIVER_START_TASK",
        doc_id="506-1",
        body_template={"task_sn": "{{state.task_sn}}"},
    ),
    "T7_receiver_end_task": Transition(
        name="T7_receiver_end_task",
        actor_role="receiver",
        method="POST",
        endpoint="/contract/receiver-task/task-end",
        fires_event="common\\components\\Contract\\Event\\AfterReceiverEndTaskEvent",
        pushes_to="publisher",
        push_type_id="CONTRACT_RECEIVER_END_TASK",
        doc_id="506-2",
        body_template={"task_sn": "{{state.task_sn}}"},
    ),

    # ===== 5 個 publisher 觸發 =====
    "T3a_publisher_accept_apply": Transition(
        name="T3a_publisher_accept_apply",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/task-match/accept",
        fires_event="common\\components\\Contract\\Event\\AfterAcceptReceiverApplyEvent",
        pushes_to="receiver",
        push_type_id="CONTRACT_PUBLISHER_ACCEPT_APPLY",
        doc_id="407-1",
        body_template={"task_sn": "{{state.task_sn}}", "receiver_id": "{{receiver.user_id}}"},
    ),
    "T3b_publisher_decline_apply": Transition(
        name="T3b_publisher_decline_apply",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/task-match/decline",
        fires_event="common\\components\\Contract\\Event\\AfterDeclineReceiverApplyEvent",
        pushes_to="receiver",
        push_type_id="CONTRACT_PUBLISHER_DECLINE_APPLY",
        doc_id="407-2",
        body_template={"task_sn": "{{state.task_sn}}", "receiver_id": "{{receiver.user_id}}"},
    ),
    "T4_publisher_cancel_hire": Transition(
        name="T4_publisher_cancel_hire",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/task-match/cancel-hire",
        fires_event="common\\components\\Contract\\Event\\AfterCancelHireReceiverApplyEvent",
        pushes_to="receiver",
        push_type_id="CONTRACT_PUBLISHER_CANCEL_HIRE",
        doc_id="407-4",
        body_template={"task_sn": "{{state.task_sn}}", "receiver_id": "{{receiver.user_id}}"},
    ),
    "T8_publisher_pass_task": Transition(
        name="T8_publisher_pass_task",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/publisher/pass-receiver-task",
        fires_event="common\\components\\Contract\\Event\\AfterPassReceiverTaskEvent",
        pushes_to="receiver",
        push_type_id="CONTRACT_PUBLISHER_PASS_TASK",
        doc_id="408-2",
        body_template={"task_sn": "{{state.task_sn}}", "receiver_id": "{{receiver.user_id}}"},
    ),
    "T8b_publisher_reject_task": Transition(
        name="T8b_publisher_reject_task",
        actor_role="publisher",
        method="POST",
        endpoint="/contract/publisher/reject-receiver-task",
        fires_event="common\\components\\Contract\\Event\\AfterRejectReceiverTaskEvent",
        pushes_to="receiver",
        push_type_id="CONTRACT_PUBLISHER_REJECT_TASK",
        doc_id="408-3",
        body_template={"task_sn": "{{state.task_sn}}", "receiver_id": "{{receiver.user_id}}"},
    ),
}


def get(name: str) -> Transition:
    if name not in TRANSITIONS:
        raise KeyError(f"unknown transition: {name}. available: {sorted(TRANSITIONS)}")
    return TRANSITIONS[name]
