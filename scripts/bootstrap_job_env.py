#!/usr/bin/env python3
"""為「工作」系統回歸測試 bootstrap 雇主測試資料（idempotent）。

背景
----
api.dev.worky.com.tw 解析到 127.0.0.1，是本地 worky；其 DB 名稱由 git 分支動態決定
（common/config/main-local.php）。目前分支 next-v31x* → 實際寫入 `worky_next_v31x`。
該庫的特性：
  - labor 審核帳號 236/276 存在且可登入（role_id=10）。
  - 完全沒有雇主（s_employers 為空），也沒有工作（s_jobs 為空）。
  - **但 s_jobs 的子表是孤兒滿載**：s_job_extras / s_labor_match_jobs / s_labor_jobs
    各有上萬筆殘留（job_id 最高 ~20781），而 s_jobs 被清空、AUTO_INCREMENT 從小值起算
    → 新發佈的工作會撞到 s_job_extras 的 PRIMARY(job_id)。

本腳本做三件事（每件都先檢查、可重複執行）：
  1. 從 worky_next_v30x 複製審核雇主 129（+ AUDIT_USER 角色 + 主店鋪 70）到目標庫。
     兩庫 s_employers / s_employer_roles / s_shops schema 完全一致，故可直接 INSERT...SELECT。
  2. 把 s_jobs AUTO_INCREMENT 抬高到所有 job 子表的 job_id 最大值之上，避開孤兒碰撞。
  3. 驗證雇主能透過 API 登入。

來源庫（v30x）保留不動；只寫目標庫（預設 = .env 的 WORKY_DB_NAME）。

用法
----
    source .venv/bin/activate
    python scripts/bootstrap_job_env.py            # 對 .env 指定的庫
    python scripts/bootstrap_job_env.py --check     # 只檢查、不寫入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from worky_regression.config import Settings          # noqa: E402
from worky_regression.verifier import DBVerifier        # noqa: E402
from worky_regression.client import WorkyClient         # noqa: E402
from worky_regression.actor import Actor, LoginFailedError  # noqa: E402

SOURCE_DB = "worky_next_v30x"   # 已有完整審核雇主的快照庫
EMPLOYER_ID = 129               # 審核雇主（phone 0923113000，AUDIT_USER）
SHOP_ID = 70                    # 該雇主主店鋪（已驗證 validation_type=2）
JOB_CHILD_TABLES = ["s_job_extras", "s_labor_match_jobs",
                    "s_labor_jobs", "s_job_modified_logs"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只檢查、不寫入")
    args = ap.parse_args()

    s = Settings.from_env()
    db = DBVerifier(s)
    target = s.db_name
    print(f"目標庫: {target} @ {s.db_host}   來源庫: {SOURCE_DB}")
    if target == SOURCE_DB:
        print("！目標庫等於來源庫，請確認 .env WORKY_DB_NAME 指向要測試的庫（如 worky_next_v31x）。")
        return 2

    # 0) 來源雇主存在性
    src = db.query_one(
        f"SELECT id, phone FROM {SOURCE_DB}.s_employers WHERE id=%s", (EMPLOYER_ID,)
    )
    if not src:
        print(f"！來源庫 {SOURCE_DB} 沒有雇主 {EMPLOYER_ID}，無法複製。")
        return 2
    phone = src["phone"]

    # 1) 複製雇主
    if db.query_one("SELECT 1 FROM s_employers WHERE id=%s", (EMPLOYER_ID,)):
        print(f"✓ 雇主 {EMPLOYER_ID} 已存在")
    elif args.check:
        print(f"· [check] 將複製雇主 {EMPLOYER_ID}")
    else:
        db.execute(f"INSERT INTO {target}.s_employers "
                   f"SELECT * FROM {SOURCE_DB}.s_employers WHERE id={EMPLOYER_ID}")
        print(f"＋ 已複製雇主 {EMPLOYER_ID}")

    # 2) 複製 AUDIT_USER 角色（role_id=10），不帶原 PK 以免撞號
    has_role = db.query_one(
        "SELECT 1 FROM s_employer_roles WHERE employer_id=%s AND role_id=10 AND published=1",
        (EMPLOYER_ID,),
    )
    if has_role:
        print("✓ AUDIT_USER 角色已存在")
    elif args.check:
        print("· [check] 將授予 AUDIT_USER 角色")
    else:
        db.execute(
            f"INSERT INTO {target}.s_employer_roles "
            "(employer_id,role_id,published,created_at,updated_at,created_by,updated_by) "
            f"SELECT employer_id,role_id,published,created_at,updated_at,created_by,updated_by "
            f"FROM {SOURCE_DB}.s_employer_roles "
            f"WHERE employer_id={EMPLOYER_ID} AND role_id=10"
        )
        print("＋ 已授予 AUDIT_USER 角色")

    # 3) 複製主店鋪
    if db.query_one("SELECT 1 FROM s_shops WHERE id=%s", (SHOP_ID,)):
        print(f"✓ 店鋪 {SHOP_ID} 已存在")
    elif args.check:
        print(f"· [check] 將複製店鋪 {SHOP_ID}")
    else:
        db.execute(f"INSERT INTO {target}.s_shops "
                   f"SELECT * FROM {SOURCE_DB}.s_shops WHERE id={SHOP_ID}")
        print(f"＋ 已複製店鋪 {SHOP_ID}")

    # 4) 修正 s_jobs AUTO_INCREMENT（避開孤兒 job 子表）
    gmax = max(db.query_one(f"SELECT COALESCE(MAX(job_id),0) m FROM {t}")["m"]
               for t in JOB_CHILD_TABLES)
    cur = db.query_one(
        "SELECT AUTO_INCREMENT a FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='s_jobs'", (target,)
    )["a"]
    floor = gmax + 100
    if cur and cur > gmax:
        print(f"✓ s_jobs AUTO_INCREMENT={cur}（已高於孤兒 job_id 上限 {gmax}）")
    elif args.check:
        print(f"· [check] 將把 s_jobs AUTO_INCREMENT 由 {cur} 抬到 {floor}（孤兒上限 {gmax}）")
    else:
        db.execute(f"ALTER TABLE s_jobs AUTO_INCREMENT={floor}")
        print(f"＋ s_jobs AUTO_INCREMENT {cur} → {floor}")

    # 5) 驗證 API 登入
    try:
        c = WorkyClient(s, user_type=1)
        Actor(role="employer", user_type=1, phone=phone,
              user_id=EMPLOYER_ID, client=c).login(audit_code=s.audit_sms_code)
        print(f"✓ 雇主 API 登入成功（phone={phone}）")
    except LoginFailedError as e:
        print(f"！雇主登入失敗：{e}")
        return 1

    print("\nbootstrap 完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
