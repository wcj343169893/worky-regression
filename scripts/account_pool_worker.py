#!/usr/bin/env python3
"""帳號池動態補池 worker：偵測可配發數，太少時自動補回一批。

設計重點
--------
- **零侵入被測倉**：池是固定 audit 種子帳號（不註冊新帳號）。「補一批」= 先回收過期租約，
  若某角色可配發數仍 < 低標，跑 provision()（解停權 / 上架 audit role + sync_caps），
  把流失（被租走逾時、殘留停權、caps 掉了）的種子帳號救回 available。
- **與看板 / runner 解耦**：可獨立起停。判定與動作都收在 `AccountPool.top_up()`，
  worker 只負責輪詢與記錄。
- **每角色獨立判定**：labor / employer 各看自己的可配發數，任一不足即觸發 provision。

用法
----
    source .venv/bin/activate
    python scripts/account_pool_worker.py                 # 持續輪詢（預設 60s）
    python scripts/account_pool_worker.py --once          # 只檢查/補一次就退出
    python scripts/account_pool_worker.py --interval 120  # 自訂輪詢秒數
    python scripts/account_pool_worker.py --min-available 3   # 每角色可用低標（預設 3）
    python scripts/account_pool_worker.py --no-heal       # 補池時不校正硬狀態（只回收 + sync）
    # 背景常駐（-u 才看得到即時 log）：
    nohup python -u scripts/account_pool_worker.py > logs/account_pool_worker.log 2>&1 &
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 讓 `python scripts/account_pool_worker.py` 直接可 import 套件
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from worky_regression.config import Settings          # noqa: E402
from worky_regression.qa_accounts import AccountPool   # noqa: E402

DEFAULT_INTERVAL = 60.0
DEFAULT_MIN_AVAILABLE = 3


def check_once(pool: AccountPool, *, min_available: int, heal: bool) -> dict:
    """檢查並補池一次，回傳 top_up 摘要並印出人話版。"""
    r = pool.top_up(min_available=min_available, heal=heal)
    before = r.get("before") or {}
    targets = r.get("targets") or {}
    desc = ", ".join(f"{role}={cnt}" for role, cnt in sorted(before.items())) or "(空池)"
    # 各角色目標已按種子容量上限調整（如 employer 只 1 個，目標就是 1，不是 min_available）
    tdesc = ", ".join(f"{role}={t}" for role, t in sorted(targets.items()))
    print(f"[pool-worker] 可配發數 {desc}（各角色目標 {tdesc}）回收過期租約 {r.get('reclaimed', 0)} 個")
    if r.get("provisioned") is not None:
        low = r.get("low") or {}
        after = r.get("after") or {}
        adesc = ", ".join(f"{role}={cnt}" for role, cnt in sorted(after.items()))
        print(f"[pool-worker] 角色不足 {low} → 已 provision 補池；補後可配發數 {adesc}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="帳號池動態補池 worker")
    ap.add_argument("--once", action="store_true", help="只檢查/補一次就退出")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"輪詢秒數（預設 {DEFAULT_INTERVAL:.0f}）")
    ap.add_argument("--min-available", type=int, default=DEFAULT_MIN_AVAILABLE,
                    help=f"每角色可配發數低標，低於即補池（預設 {DEFAULT_MIN_AVAILABLE}）")
    ap.add_argument("--no-heal", dest="heal", action="store_false",
                    help="補池時不校正硬狀態（只回收過期租約 + sync_caps）")
    ap.set_defaults(heal=True)
    args = ap.parse_args()

    settings = Settings.from_env()
    pool = AccountPool(settings)

    print(f"[pool-worker] 啟動。QA DB={settings.qa_db_name}@{settings.db_host} "
          f"min_available={args.min_available} heal={args.heal} interval={args.interval}s")
    if args.once:
        check_once(pool, min_available=args.min_available, heal=args.heal)
        return 0

    try:
        while True:
            try:
                check_once(pool, min_available=args.min_available, heal=args.heal)
            except Exception as e:  # noqa: BLE001 — 單次失敗不該打掛輪詢（如工作庫暫時不可達）
                print(f"[pool-worker] 補池出錯：{type(e).__name__}: {e}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[pool-worker] 已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
