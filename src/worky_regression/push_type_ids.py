"""Push notification Type 常量 → 實際 type_id 映射。

對應 PHP `common\\components\\PushNotification\\Type` 常量值（worky_next_v30x）。
驗證來源：每個 Contract/EventHandler/After*/PushNotification.php 的 typeId 參數。

特殊備註：
- T5 receiver cancel：handler 依時間動態切換 type:
    normal      → CONTRACT_RECEIVER_CANCEL_ACCEPTED (20039)
    last-minute → CONTRACT_CANCEL_LAST_MINUTE       (20046)
  測試環境通常會用「正常時段」的 fixture，預期 20039。
- T4 cancel-hire：handler 用 CONTRACT_RECEIVER_TASK_CANCELED (20053)
  （命名不太對稱，但程式碼確實如此 — 不要被名字誤導）
"""

PUSH_TYPE_IDS: dict[str, int] = {
    # === T2 接案者申請 → 發案者收 push ===
    "CONTRACT_RECEIVER_APPLY_TASK": 20038,            # Type::CONTRACT_RECEIVER_APPLY

    # === T3 接案者接受發案者邀請 → 發案者收 push ===
    "CONTRACT_RECEIVER_ACCEPT_INVITE": 20062,         # Type::CONTRACT_PUBLISHER_INVITE_ACCEPTED

    # === T3a 發案者同意申請 → 接案者收 push ===
    "CONTRACT_PUBLISHER_ACCEPT_APPLY": 20051,         # Type::CONTRACT_RECEIVER_APPLY_ACCEPTED

    # === T3b 發案者婉拒申請 → 接案者收 push ===
    "CONTRACT_PUBLISHER_DECLINE_APPLY": 20052,        # Type::CONTRACT_RECEIVER_APPLY_REJECTED

    # === T4 發案者取消錄取 → 接案者收 push ===
    "CONTRACT_PUBLISHER_CANCEL_HIRE": 20053,          # Type::CONTRACT_RECEIVER_TASK_CANCELED

    # === T5 接案者取消任務 → 發案者收 push（正常時段）===
    "CONTRACT_RECEIVER_CANCEL_TASK": 20039,           # Type::CONTRACT_RECEIVER_CANCEL_ACCEPTED
    "CONTRACT_RECEIVER_CANCEL_TASK_LAST_MINUTE": 20046,  # Type::CONTRACT_CANCEL_LAST_MINUTE

    # === T6 接案者開始任務 → 發案者收 push ===
    "CONTRACT_RECEIVER_START_TASK": 20048,            # Type::CONTRACT_PUBLISHER_NOTIFY_TASK_START

    # === T7 接案者結束任務 → 發案者收 push ===
    "CONTRACT_RECEIVER_END_TASK": 20049,              # Type::CONTRACT_PUBLISHER_NOTIFY_TASK_END

    # === T8 發案者通過任務完成 → 接案者收 push ===
    "CONTRACT_PUBLISHER_PASS_TASK": 20057,            # Type::CONTRACT_RECEIVER_TASK_SUCCESS

    # === T8b 發案者駁回任務完成 → 接案者收 push ===
    "CONTRACT_PUBLISHER_REJECT_TASK": 20058,          # Type::CONTRACT_RECEIVER_TASK_FAIL
}
