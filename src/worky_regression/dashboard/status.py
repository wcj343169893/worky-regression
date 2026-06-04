"""承攬制任務狀態 → 統一進度碼。

這份檔案是主倉 `common/base/Enums/Contract/*` 與
`common/base/Enums/Contract/PublisherTaskStatus.php` 的 Python 移植，
讓看板能用「發案者視角的統一進度」呈現每個任務目前走到哪一關。

只要主倉那幾個 enum 改了，這裡要同步（與 transitions.py / push_type_ids.py 同樣的規矩）。
"""
from __future__ import annotations

from dataclasses import dataclass

# ── 原始欄位狀態對照（s_contract_tasks.status / pay_status 等）────────────────

TASK_STATUS = {
    0: "未發布", 1: "媒合中", 2: "招募結束", 21: "招募結束(額滿)", 22: "招募結束(截止)",
    3: "任務開始", 4: "任務結束", 41: "任務完成", 5: "任務失敗", 6: "發案者取消",
    7: "自動取消", 71: "自動取消(未繳款)", 72: "自動取消(無人)",
}

PAY_STATUS = {
    0: "無", 101: "等待付款", 102: "付款完成", 104: "準備結算",
    105: "已結算", 107: "自動取消", 108: "退款失敗",
}

# s_contract_receiver_tasks.task_status（接案者任務狀態）
RECEIVER_TASK_TASK_STATUS = {
    101: "待開始", 102: "執行中", 103: "已執行/待確認", 104: "駁回",
    105: "任務完成", 106: "任務失敗", 107: "已結算", 108: "已取消",
}

# s_contract_receiver_tasks.status（接案者上工狀態）
RECEIVER_TASK_STATUS = {
    0: "-", 1: "前往工作", 2: "任務失敗", 3: "發案者取消", 4: "接案者取消",
    5: "發案者取消紀錄", 6: "未到場失敗", 7: "未結束失敗",
}

# s_contract_receiver_match_tasks.status（申請/媒合狀態）
RECEIVER_MATCH_STATUS = {
    1: "媒合中", 2: "接案者同意", 3: "接案者婉拒", 4: "接案者申請",
    5: "接案者取消申請", 6: "發案者同意", 7: "發案者婉拒", 8: "發案者取消",
    9: "接案者取消", 10: "發案者取消錄取", 11: "招募結束", 12: "備取中",
    13: "未錄取", 93: "發案者取消(未付款)", 94: "發案者取消(未錄取)",
}

# s_contract_task_change_logs.status（任務變更日誌 → 進度時間軸）
CHANGE_LOG_STATUS = {
    1: "發佈任務", 2: "招募截止", 3: "付款完成", 4: "發案者取消任務",
    5: "自動取消任務", 6: "任務失敗", 7: "任務結算", 8: "任務下架",
    101: "接案者申請任務", 102: "接案者取消申請任務", 103: "接案者同意邀請",
    104: "接案者婉拒邀請", 105: "發案者同意申請", 106: "發案者婉拒申請",
    201: "接案者取消任務", 202: "發案者取消錄用", 203: "接案者開始任務",
    204: "接案者結束任務", 205: "發案者駁回任務", 206: "發案者確認完成任務",
    207: "發案者評價", 208: "接案者評價", 209: "發案者刪除任務", 210: "接案者刪除任務",
}

PAYMENT_METHOD = {1: "FunPoint 信用卡", 2: "信用卡", 3: "ATM"}


def label(mapping: dict, value) -> str:
    if value is None:
        return "-"
    return mapping.get(int(value), f"未知({value})")


# ── 統一進度碼（PublisherTaskStatus 移植）──────────────────────────────────

# 進度碼定義（與 PublisherTaskStatus 常量一致）
P_UNKNOWN = 0
P_MATCHING = 1        # 媒合中
P_HANDLE = 2          # 處理中（招募截止、建立金流訂單中）
P_WAITING_PAY = 3     # 待付款
P_WAITING_START = 4   # 待開始
P_PROCESSING = 5      # 執行中
P_WAITING_CONFIRM = 6 # 待確認
P_TASK_COMPLETED = 7  # 任務完成
P_REJECTED = 8        # 駁回
P_TASK_FAILED = 9     # 任務失敗
P_CANCELED = 10       # 已取消
P_RECORD_ONLY = 99    # 僅記錄（已執行過，但主倉工作庫已查不到該 SN→降級顯示）

PROGRESS_TITLE = {
    P_UNKNOWN: "未知", P_MATCHING: "媒合中", P_HANDLE: "處理中",
    P_WAITING_PAY: "待付款", P_WAITING_START: "待開始", P_PROCESSING: "執行中",
    P_WAITING_CONFIRM: "待確認", P_TASK_COMPLETED: "任務完成",
    P_REJECTED: "駁回", P_TASK_FAILED: "任務失敗", P_CANCELED: "已取消",
    P_RECORD_ONLY: "僅記錄",
}

PROGRESS_HINT = {
    P_MATCHING: "正在為您匹配優質接案者，請耐心等待",
    P_HANDLE: "系統正在建立金流訂單中，請耐心等待",
    P_WAITING_PAY: "已選定接案者，請及時付款",
    P_WAITING_START: "已付款，待接案者開始任務",
    P_PROCESSING: "接案者正在執行任務，請耐心等待",
    P_WAITING_CONFIRM: "任務已完成，請確認驗收結果",
    P_TASK_COMPLETED: "任務已完成，期待下次與您合作",
    P_REJECTED: "可於任務表定結束時間後 72 小時更改為通過",
    P_TASK_FAILED: "接案者未完成任務，任務已結束",
    P_CANCELED: "任務已取消",
}

# 進度碼 ← 對應回歸框架的 transition（把看板綁回測試框架）
PROGRESS_TRANSITION = {
    P_MATCHING: "T1 發佈 / T2 申請 / T3a 同意",
    P_HANDLE: "招募截止後建單",
    P_WAITING_PAY: "等待付款",
    P_WAITING_START: "付款完成（db_exec pay_status=102）",
    P_PROCESSING: "T6 開始任務",
    P_WAITING_CONFIRM: "T7 結束任務",
    P_TASK_COMPLETED: "T8 通過任務",
    P_REJECTED: "T8b 駁回任務",
    P_TASK_FAILED: "任務失敗",
    P_CANCELED: "T4 取消錄用 / T5 接案者取消",
}

# Happy-path 線性階段（給前端 stepper 用）。處理中(2) 折進媒合中那一格。
PROGRESS_STEPPER = [P_MATCHING, P_WAITING_PAY, P_WAITING_START,
                    P_PROCESSING, P_WAITING_CONFIRM, P_TASK_COMPLETED]

# 分支/終止狀態（不在線性 stepper 上，單獨標紅/標灰）
PROGRESS_BRANCH = {P_REJECTED, P_TASK_FAILED, P_CANCELED}


@dataclass
class Progress:
    code: int
    title: str
    hint: str
    transition: str
    stage_index: int   # 在 PROGRESS_STEPPER 中的索引；分支/未知為 -1
    is_branch: bool
    is_terminal: bool

    def to_dict(self) -> dict:
        return {
            "code": self.code, "title": self.title, "hint": self.hint,
            "transition": self.transition, "stage_index": self.stage_index,
            "is_branch": self.is_branch, "is_terminal": self.is_terminal,
        }


def _stage_index(code: int) -> int:
    if code == P_HANDLE:
        return 0  # 處理中視覺上仍在「媒合中」這一格
    try:
        return PROGRESS_STEPPER.index(code)
    except ValueError:
        return -1


def derive_progress(*, task_status: int, pay_status: int, recruit_deadline: int,
                    receiver_task_status: int | None, now: int) -> Progress:
    """移植 PublisherTaskStatus::getCode()，依任務/付款/接案者任務狀態組合出統一進度碼。"""
    ts = int(task_status or 0)
    ps = int(pay_status or 0)
    rd = int(recruit_deadline or 0)
    rtt = int(receiver_task_status or 0)

    # 21/22（額滿/截止）視為招募結束家族；1/2 為媒合家族
    matching_family = ts in (1, 2, 21, 22)
    stop_recruit = ts in (2, 21, 22)

    if matching_family and rd > now and ps == 0:
        code = P_MATCHING
    elif matching_family and rd <= now and ps == 0:
        code = P_HANDLE
    elif stop_recruit and ps == 101:
        code = P_WAITING_PAY
    elif stop_recruit and ps == 102:
        code = P_WAITING_START
    elif ts == 3 and rtt == 102:
        code = P_PROCESSING
    elif ts == 3 and rtt == 103:
        code = P_WAITING_CONFIRM
    elif ts in (4, 41) and rtt in (105, 107):
        code = P_TASK_COMPLETED
    elif ts == 3 and rtt == 104:
        code = P_REJECTED
    elif ts in (5,):
        code = P_TASK_FAILED
    elif ts in (6, 7, 71, 72):
        code = P_CANCELED
    else:
        code = P_UNKNOWN

    return Progress(
        code=code,
        title=PROGRESS_TITLE.get(code, "未知"),
        hint=PROGRESS_HINT.get(code, ""),
        transition=PROGRESS_TRANSITION.get(code, ""),
        stage_index=_stage_index(code),
        is_branch=code in PROGRESS_BRANCH,
        is_terminal=code in (P_TASK_COMPLETED, P_TASK_FAILED, P_CANCELED),
    )


def record_only_progress() -> Progress:
    """降級進度（承攬制）：已執行過但主倉查不到該 SN，仍要列出而不 crash。"""
    return Progress(
        code=P_RECORD_ONLY,
        title=PROGRESS_TITLE[P_RECORD_ONLY],
        hint="本框架曾執行過此任務，但主倉工作庫目前查無此 SN（可能已刪除或分庫差異）。",
        transition="",
        stage_index=-1,
        is_branch=False,
        is_terminal=False,
    )


# ══════════════════════════════════════════════════════════════════════════
# 工作系統（job）enum 對照 + 進度分類
#   來源：common/base/Enums/JobStatus.php / JobPayStatus.php / LaborJobStatus.php
#   以及 endpoints.yaml 的 enums 區塊（LaborJob.status / LaborMatchJob.status）。
# ══════════════════════════════════════════════════════════════════════════

JOB_STATUS = {
    0: "未發佈", 1: "媒合中", 8: "刪除", 9: "商家取消", 15: "招募結束",
    21: "工作開始", 25: "工作結束", 27: "工作失敗", 31: "自動取消",
    91: "招募額滿", 92: "招募時間截止", 93: "自動取消(未付款)", 94: "自動取消(未錄取)",
}

JOB_PAY_STATUS = {
    0: "無", 1: "等待付款", 2: "付款完成", 3: "準備結算",
    4: "已結算", 5: "結算失敗", 6: "待母單結算", 7: "付款中", 31: "自動取消",
}

# s_labor_jobs.status（打工夥伴上工狀態）
LABOR_JOB_STATUS = {1: "上工", 8: "商家取消", 9: "打工夥伴取消", 10: "商家取消錄取"}
# s_labor_jobs.job_status（工作執行狀態）
LABOR_JOB_JOB_STATUS = {
    0: "無", 1: "待上工", 2: "上工中", 3: "已下工", 4: "已結算", 8: "申請中", 9: "已取消",
}
# s_labor_match_jobs.status（申請/媒合狀態）
LABOR_MATCH_STATUS = {
    1: "媒合中", 2: "夥伴同意", 3: "夥伴婉拒", 4: "夥伴申請", 5: "夥伴取消申請",
    6: "商家同意", 7: "商家婉拒", 8: "商家取消", 9: "夥伴取消", 10: "商家取消錄取",
    15: "招募結束", 16: "備取中",
}

# 工作進度分類（給看板色塊；不像承攬制那樣組合 pay/receiver，直接由 JobStatus 映射）
#   category: matching / recruited / running / done / canceled / failed / draft
JOB_PROGRESS = {
    0:  ("draft", "未發佈"),
    1:  ("matching", "媒合中"),
    15: ("recruited", "招募結束"), 91: ("recruited", "招募額滿"), 92: ("recruited", "招募截止"),
    21: ("running", "工作開始"),
    25: ("done", "工作結束"),
    27: ("failed", "工作失敗"),
    8:  ("canceled", "刪除"), 9: ("canceled", "商家取消"), 31: ("canceled", "自動取消"),
    93: ("canceled", "自動取消(未付款)"), 94: ("canceled", "自動取消(未錄取)"),
}
JOB_PROGRESS_ORDER = [
    ("draft", "未發佈"), ("matching", "媒合中"), ("recruited", "招募結束"),
    ("running", "工作開始"), ("done", "工作結束"),
    ("failed", "工作失敗"), ("canceled", "取消/刪除"),
    ("record_only", "僅記錄"),
]

# category → 對應的 JobStatus 值（讓看板的進度 chip 能直接走 SQL WHERE 過濾全集）
CATEGORY_STATUSES: dict[str, list[int]] = {}
for _code, (_cat, _t) in JOB_PROGRESS.items():
    CATEGORY_STATUSES.setdefault(_cat, []).append(_code)


def job_progress(status) -> dict:
    cat, title = JOB_PROGRESS.get(int(status or 0), ("draft", label(JOB_STATUS, status)))
    return {"category": cat, "title": title, "status": int(status or 0),
            "status_label": label(JOB_STATUS, status)}


def job_record_only_progress() -> dict:
    """降級進度（工作）：已執行過但主倉查不到該 job_sn。"""
    return {"category": "record_only", "title": "僅記錄", "status": None,
            "status_label": "僅記錄"}


def meta_payload() -> dict:
    """提供給前端的所有 enum 對照與進度定義。"""
    return {
        "task_status": TASK_STATUS,
        "pay_status": PAY_STATUS,
        "receiver_task_task_status": RECEIVER_TASK_TASK_STATUS,
        "receiver_task_status": RECEIVER_TASK_STATUS,
        "receiver_match_status": RECEIVER_MATCH_STATUS,
        "change_log_status": CHANGE_LOG_STATUS,
        "payment_method": PAYMENT_METHOD,
        "progress_title": PROGRESS_TITLE,
        "progress_hint": PROGRESS_HINT,
        "progress_transition": PROGRESS_TRANSITION,
        "progress_stepper": PROGRESS_STEPPER,
        "progress_branch": sorted(PROGRESS_BRANCH),
    }
