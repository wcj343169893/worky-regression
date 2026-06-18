#!/usr/bin/env python3
"""長延時 run 的喚醒進程（Tier 2「掛起→喚醒」的後半段）。

工作排在很久之後（如「明天 13:00」開工）的用例，runner 不會死等：跑完「現在」段
（發佈/申請/錄取/上班卡）後，wait_until 發現距表定開工還很久，就把這次執行**冷凍**成
``qa_runs.status='waiting'``（落 checkpoint：全量 state.vars + actor 快照 + 系統/用例），
並釋放帳號租約（不能抱 24h）。

本進程獨立於看板 server 之外，輪詢「到點該醒」的 waiting run（``resume_at<=now``）：
  1. 原子搶占（waiting→resuming，多 worker 不重領）。
  2. 依 checkpoint 的 actor 快照**重新拿回同一批帳號**（已綁該 job_sn，不能換人）並登入
     （池 token 快取：有效就用、過期才刷/重登）。
  3. 從 checkpoint 還原 state.vars，從掛起的那一步續跑剩餘步驟（沿用原 run_id，續寫 steps）。
同一支用例可多次掛起/喚醒（先等開工、再等近結束）——每次喚醒都從「上次掛起的 wait_until」
重入，剩餘時間已縮短，到夠近時 inline 等一下即過。

啟動方式（與 markup_worker 一致，建議走 user systemd 常駐）：

    python -u scripts/resume_worker.py                 # 預設 60s 輪詢
    python -u scripts/resume_worker.py --interval 30
    python -u scripts/resume_worker.py --once          # 處理一筆就退出（debug）
"""
from __future__ import annotations

import argparse
import fcntl
import sys
import time
from pathlib import Path

import yaml

# 讓 `python scripts/resume_worker.py` 直接可 import 套件
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from worky_regression.autotest import actor_swapper_for, actors_from_snapshot  # noqa: E402
from worky_regression.config import Settings  # noqa: E402
from worky_regression.qa_store import QAStore  # noqa: E402
from worky_regression.recorder import RecordingRunner  # noqa: E402
from worky_regression.verifier import DBVerifier  # noqa: E402

LOCK_FILE = Path(__file__).resolve().parents[1] / "logs" / "resume_worker.lock"


def acquire_singleton_lock():
    """單例鎖：flock 不阻塞搶占；搶不到表示已有 worker 在跑，直接退出。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")  # noqa: SIM115 — handle 要活到行程結束
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[resume-worker] 已有另一個 resume_worker 在跑（單例鎖被持有），本實例退出。")
        raise SystemExit(1)
    fh.write(f"{time.time()}\n")
    fh.flush()
    return fh


def process_one(settings: Settings, qa: QAStore) -> bool:
    """喚醒並續跑一筆到點的 waiting run；無到點者回 False。"""
    claim = qa.claim_due_waiting_run()
    if not claim:
        return False
    run_id = claim["run_id"]
    system = claim["system"]
    cp = claim["checkpoint"] or {}
    print(f"[resume-worker] 喚醒 run={run_id} case={claim['case_id']} system={system} "
          f"續跑 step={claim['resume_step_index']}")

    # 真機軌（app）不會產生 wait_until 掛起；非 job/contract 一律不處理，標 failed 收場
    if system not in ("job", "contract"):
        print(f"[resume-worker] 不支援的系統 {system!r}，標 failed。")
        qa.set_run_status(run_id, "failed")
        return True

    yaml_text = qa.get_case_yaml(claim["case_id"])
    if not yaml_text:
        print(f"[resume-worker] 找不到用例 {claim['case_id']} 的 YAML（可能已刪），標 failed。")
        qa.set_run_status(run_id, "failed")
        return True
    spec = yaml.safe_load(yaml_text)

    snapshot = cp.get("actors") or {}
    if not snapshot:
        print(f"[resume-worker] run={run_id} checkpoint 無 actor 快照，無法重建，標 failed。")
        qa.set_run_status(run_id, "failed")
        return True

    owner = f"resume-{run_id}"
    actors, pool = actors_from_snapshot(settings, system, snapshot, owner=owner)
    try:
        db = DBVerifier(settings.for_system(system))
        runner = RecordingRunner(db, qa_store=qa, system=system,
                                 actor_swapper=actor_swapper_for(system, settings))
        resume = {
            "run_id": run_id,
            "started_at": cp.get("started_at"),
            "vars": cp.get("vars") or {},
            "resume_step_index": claim["resume_step_index"],
        }
        result = runner.run(spec, actors=actors, resume=resume)
        print(f"[resume-worker] run={run_id} 續跑結果：{result.status}"
              + (f"（再次掛起，resume_at={getattr(result, 'run_id', '')}）"
                 if result.status == "waiting" else ""))
    finally:
        # 租約歸還須與上鎖同作用域（for_system 的 db_name）——actors_from_snapshot 已用該作用域
        pool.release(owner)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="長延時 run 的喚醒/續跑進程")
    ap.add_argument("--once", action="store_true", help="處理一筆就退出（debug）")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="無到點 run 時的輪詢秒數（預設 60）")
    args = ap.parse_args()

    _lock = acquire_singleton_lock()  # noqa: F841 — handle 須存活到行程結束（持鎖）
    settings = Settings.from_env()
    qa = QAStore(settings)
    qa.migrate()  # 確保 resume 欄位等 schema 在
    reset = qa.reset_resuming_runs()  # 上次 worker 領取後沒跑完就掛了的，退回 waiting 重試
    if reset:
        print(f"[resume-worker] 啟動收斂：{reset} 筆卡在 resuming 退回 waiting。")
    print(f"[resume-worker] 啟動（單例鎖已持有）。QA DB={settings.qa_db_name}@{settings.db_host} "
          f"interval={args.interval}s")

    if args.once:
        if not process_one(settings, qa):
            print("[resume-worker] 無到點 run。")
        return 0
    try:
        while True:
            try:
                did = process_one(settings, qa)
            except Exception as e:  # noqa: BLE001 — 單筆失敗不該打掛輪詢
                print(f"[resume-worker] 處理出錯：{e}")
                did = False
            if not did:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[resume-worker] 已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
