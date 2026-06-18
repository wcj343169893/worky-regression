#!/usr/bin/env python3
"""把一筆「假失敗」的 job 打卡 run 重建成 waiting，交給 resume_worker 在原 job 上續跑。

背景：長延時打卡用例在掛起/喚醒機制（Tier 2）落地前，會因 `wait_api working_status:1`
死等逾時而 `failed`——其實 job 已發佈、夥伴已打上班卡，只是「等不到開工」。用例修成
`wait_until` 後，這筆失敗的歷史 run 仍是終態、沒 checkpoint。本腳本針對這種 run 重建最小
checkpoint（job_sn 從 step0 觀測值取回、時段錨由 started_at + 用例 vars 重算、帳號用 run
的 actor 快照），刪掉失敗步起的殘留 step，翻成 waiting，resume_worker 到開工時就會在**原
job** 上從失敗步續跑打卡/評價，不必重發 job。

只適用「開工尚未到」的 run（否則搶救無意義）。用法：

    python scripts/revive_failed_run.py --run-id <run_id>            # 預覽
    python scripts/revive_failed_run.py --run-id <run_id> --commit   # 實際寫入
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from worky_regression import qa_models  # noqa: E402
from worky_regression.config import Settings  # noqa: E402
from worky_regression.qa_store import QAStore  # noqa: E402
from worky_regression.runner import (  # noqa: E402
    _job_clock_anchors, _job_slot_vars, _relative_slot,
)


def rebuild_job_vars(started_at: int, case_vars: dict, job_sn: str) -> dict:
    """以 started_at 當「發佈當下」重算 init_state 會產生的時段 vars（含 job_start_at/end_at）。"""
    now = started_at
    v: dict = {
        "run_id": "revived",
        "start_time": now + 90000, "end_time": now + 90000 + 3700,
        **_job_slot_vars(now),
        "job_recruit_count": 1,
    }
    v.update(case_vars or {})
    wm = int(v.get("job_work_minutes", 120))
    after = v.get("job_start_after_minutes")
    tod = v.get("job_start_time_of_day")
    if after is not None:
        v.update(_relative_slot(now, int(after), wm))
    elif tod:
        from worky_regression.runner import _anchor_today_slot
        v.update(_anchor_today_slot(now, str(tod), wm))
    v.update(_job_clock_anchors(v))
    v["job_sn"] = job_sn
    return v


def main() -> int:
    ap = argparse.ArgumentParser(description="把假失敗的 job 打卡 run 重建成 waiting 續跑")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--commit", action="store_true", help="實際寫入（預設只預覽）")
    args = ap.parse_args()

    s = Settings.from_env()
    qa = QAStore(s)
    eng = qa_models.get_engine(s)

    with eng.connect() as conn:
        run = conn.execute(text(
            "SELECT run_id, case_id, `system`, status, failed_at, started_at, actors "
            "FROM qa_runs WHERE run_id=:r"), {"r": args.run_id}).first()
    if not run:
        print(f"找不到 run {args.run_id}")
        return 2
    if run.status != "failed" or run.failed_at is None:
        print(f"run 狀態為 {run.status}（failed_at={run.failed_at}）；只搶救 failed 且有 failed_at 的。")
        return 2
    if run.system != "job":
        print(f"目前只支援 job 系統（本筆 system={run.system}）。")
        return 2

    actors = run.actors
    actors = json.loads(actors) if isinstance(actors, str) else (actors or {})

    yaml_text = qa.get_case_yaml(run.case_id)
    if not yaml_text:
        print(f"找不到用例 {run.case_id} 的 YAML（DB）。")
        return 2
    spec = yaml.safe_load(yaml_text)
    fa = int(run.failed_at)
    if fa >= len(spec["path"]) or "wait_until" not in spec["path"][fa]:
        print(f"失敗步 {fa} 在現用例不是 wait_until（用例可能未轉換或步序變動）。"
              f"step={spec['path'][fa] if fa < len(spec['path']) else 'OOB'}")
        return 2

    # 取回 job_sn（step0 J1 發佈的觀測值）
    steps = qa.load_run_steps(args.run_id)
    job_sn = None
    for st in steps:
        saved = (st.get("observations") or {}).get("saved") or {}
        if saved.get("job_sn"):
            job_sn = str(saved["job_sn"]); break
    if not job_sn:
        print("step 觀測值裡找不到 job_sn，無法續跑（原 job 不可知）。")
        return 2

    vars_ = rebuild_job_vars(int(run.started_at), spec.get("vars") or {}, job_sn)
    start_at = int(vars_["job_start_at"])
    resume_at = start_at + 30
    now = int(time.time())
    print(f"run={args.run_id}\n  case={run.case_id}  job_sn={job_sn}")
    print(f"  失敗步 {fa} = {spec['path'][fa].get('wait_until')}")
    print(f"  表定開工 start_at={start_at}（距今 {(start_at-now)/3600:.1f}h）  end_at={vars_['job_end_at']}")
    print(f"  → 重建 resume_at={resume_at} resume_step_index={fa}")
    print(f"  actors={ {k: v.get('user_id') for k, v in actors.items()} }")
    if start_at <= now:
        print("  ⚠️ 開工已過，搶救無意義（resume 後會立刻打卡，可能與真實時間不符）。中止。")
        return 2

    checkpoint = {"vars": vars_, "actors": actors, "system": run.system,
                  "case_id": run.case_id, "description": "", "started_at": int(run.started_at)}

    if not args.commit:
        print("\n（預覽，未寫入。加 --commit 實際執行）")
        return 0

    # 刪掉失敗步起的殘留 step（避免 resume 收尾 insert_run 出現重複 step_index），再翻 waiting
    with eng.begin() as conn:
        deleted = conn.execute(text(
            "DELETE FROM qa_run_steps WHERE run_id=:r AND step_index>=:i"),
            {"r": args.run_id, "i": fa}).rowcount
    qa.suspend_run(run_id=args.run_id, resume_at=resume_at,
                   resume_step_index=fa, checkpoint=checkpoint)
    print(f"\n✅ 已重建成 waiting（刪除殘留 step {deleted} 筆）。"
          f"resume_worker 將於 start_at 後喚醒，在原 job {job_sn} 上續跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
