#!/usr/bin/env python3
"""真機軌（B）背景執行進程：序列化跑 system=app 的 Maestro 用例。

單裝置（實體手機 / 模擬器）一次只能被一個 maestro / 截圖 session 驅動，故真機用例不能像
API 軌那樣並行批量。本進程把真機用例**逐條序列化**跑在背景，結果照常落 worky_qa_dashboard
（看板「📱 真機」tab / 歷史沿用）。

序列化的兩道保險
----------------
1. **單例鎖**（logs/device_worker.lock，flock 不阻塞搶占）：同時只允許一個 device_worker 在跑。
2. **跨進程裝置鎖**（DeviceRunner 內，/tmp/worky-device-<id>.lock）：看板 inline 執行與本 worker
   共用同一台機時也不會撞——worker 取鎖時用長 wait 排隊（--lock-wait），看板預設取不到就快速失敗。

執行通道為 maestro **CLI**（非 MCP）：背景進程取不到 MCP 工具。見 README「真機軌（B）」。

用法
----
    source .venv/bin/activate
    python scripts/device_worker.py --list                 # 列出 system=app 用例後退出
    python scripts/device_worker.py --once                 # 把整套真機用例序列化跑一輪後退出
    python scripts/device_worker.py --case device-labor-home-smoke   # 只跑指定用例（可多個）
    python scripts/device_worker.py                        # 常駐：每 --interval 秒跑一輪
    # 背景常駐（記得 -u 才看得到即時 log）：
    nohup python -u scripts/device_worker.py > logs/device_worker.log 2>&1 &
"""
from __future__ import annotations

import argparse
import fcntl
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from worky_regression.config import Settings          # noqa: E402
from worky_regression.dashboard.cases import CaseStore  # noqa: E402

LOCK_FILE = PROJECT_ROOT / "logs" / "device_worker.lock"
# 常駐預設輪詢間隔：真機用例慢（單支數十秒～分鐘），整套跑一輪後歇久一點才再跑。
DEFAULT_INTERVAL = 1800.0
# worker 取裝置鎖的等待上限：背景排隊等看板 inline 執行讓出裝置（夠長以涵蓋一支真機用例）。
DEFAULT_LOCK_WAIT = 900.0


def acquire_singleton_lock():
    """單例鎖：搶不到表示已有 device_worker 在跑，直接退出（避免兩個 worker 搶同一台機）。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")  # noqa: SIM115 — handle 要活到行程結束
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[device-worker] 已有另一個 device_worker 在跑（單例鎖被持有），本實例退出。")
        raise SystemExit(1)
    fh.write(f"{time.time()}\n")
    fh.flush()
    return fh


def app_case_ids(cs: CaseStore, *, include_manual: bool = False) -> list[str]:
    """看板「📱 真機」tab 的全部用例 id（system=app；limit=0 取全集）。

    預設跳過 spec 標 ``auto: false`` 的用例——那些會改後端狀態（如應徵職缺），不該被
    每小時的背景輪詢重跑；看板手動「執行」或 `--case <id>` 仍可單獨跑。include_manual=True
    時不過濾（給 --case / --list 全集用）。
    """
    res = cs.list_cases(system="app", limit=0)
    ids = [it["id"] for it in res.get("items", [])]
    if include_manual:
        return ids
    kept: list[str] = []
    for cid in ids:
        found = cs._find(cid)   # (path, source, spec)；讀 spec 看 auto 旗標
        spec = found[2] if found else {}
        if spec.get("auto", True):
            kept.append(cid)
        else:
            print(f"[device-worker] 跳過 {cid}（auto: false，僅手動執行）")
    return kept


def run_one(cs: CaseStore, case_id: str, lock_wait: float) -> dict:
    """序列化跑一條真機用例（DeviceRunner 取裝置鎖排隊）；回 run 結果 dict。"""
    print(f"[device-worker] ▶ 執行 {case_id} …")
    res = cs.run_case(case_id, device_lock_wait=lock_wait)
    passed = sum(1 for s in res.get("steps", []) if s.get("status") == "passed")
    total = len(res.get("steps", []))
    print(f"[device-worker] {case_id} → {res.get('status')}（{passed}/{total}）run_id={res.get('run_id')}")
    return res


def run_suite(cs: CaseStore, ids: list[str], lock_wait: float) -> None:
    """把一組真機用例逐條序列化跑完（單條失敗不打斷整套）。"""
    if not ids:
        print("[device-worker] 無 system=app 用例可跑。")
        return
    print(f"[device-worker] 本輪共 {len(ids)} 條真機用例：{', '.join(ids)}")
    for cid in ids:
        try:
            run_one(cs, cid, lock_wait)
        except Exception as e:  # noqa: BLE001 — 單條失敗不該打斷整套
            print(f"[device-worker] {cid} 執行出錯：{e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="真機軌（B）序列化背景執行進程")
    ap.add_argument("--once", action="store_true", help="跑一輪（整套或 --case 指定）後退出")
    ap.add_argument("--case", nargs="+", metavar="ID", help="只跑指定用例 id（可多個）")
    ap.add_argument("--list", action="store_true", help="列出 system=app 用例後退出")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help=f"常駐模式每輪間隔秒數（預設 {int(DEFAULT_INTERVAL)}）")
    ap.add_argument("--lock-wait", type=float, default=DEFAULT_LOCK_WAIT,
                    help=f"取裝置鎖的等待上限秒數（預設 {int(DEFAULT_LOCK_WAIT)}）")
    args = ap.parse_args()

    settings = Settings.from_env()
    cs = CaseStore(settings)

    if args.list:
        auto_ids = set(app_case_ids(cs))                 # 會被背景輪詢的
        all_ids = app_case_ids(cs, include_manual=True)  # 全集（含 auto: false）
        print(f"[device-worker] system=app 用例（{len(all_ids)}；背景輪詢 {len(auto_ids)}）：")
        for cid in all_ids:
            print(f"  - {cid}" + ("" if cid in auto_ids else "  [auto:false 僅手動]"))
        return 0

    _lock = acquire_singleton_lock()  # noqa: F841 — handle 須存活到行程結束（持鎖）
    print(f"[device-worker] 啟動（單例鎖已持有）。QA DB={settings.qa_db_name}@{settings.db_host} "
          f"device={settings.maestro_device_id or '(adb 自動挑)'} lock_wait={args.lock_wait}s")

    def ids_this_round() -> list[str]:
        return list(args.case) if args.case else app_case_ids(cs)

    if args.once or args.case:
        run_suite(cs, ids_this_round(), args.lock_wait)
        return 0

    try:
        while True:
            run_suite(cs, ids_this_round(), args.lock_wait)
            print(f"[device-worker] 本輪結束，{int(args.interval)}s 後再跑。")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[device-worker] 已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
