"""測試帳號池：QA 自管的「帳號狀態管理 + 同步 + 配發」。

設計動機
--------
過去要跑用例得**直連工作庫**挖「哪個帳號能用」(SELECT s_labors) 並 db_exec 喬狀態，
這依賴工作庫權限、且每跑一次冒一個新守衛(認證/停權/時段…)。本模組把它收斂成三段：

  1) 供給 provision()  ── 特權、偶發：在「有工作庫/後台權限」的環境跑，校正測試帳號的
     硬狀態(上架 audit role、清測試殘留的停權)，並把每個帳號的能力探測寫進 qa_accounts。
  2) 同步 sync_caps()  ── 探測 verified/active/profile/audit_role 等能力 → 寫 caps。
  3) 配發 acquire()    ── **執行期只讀 qa_accounts**（QA 自己的庫），按 role+caps 配帳號、
     加軟租約避免並行 run 互搶。runner 的 _actors_for 改吃這裡，不再直連工作庫。

labor 與 employer（商家）都納管：role 欄位區分；employer 也有自己的能力(active/verified_shop)。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import sqlalchemy
from sqlalchemy import text

from .config import Settings
from . import qa_models


class PoolShortage(RuntimeError):
    """池中符合條件的帳號不足以配發要求的數量。"""


@dataclass
class PooledAccount:
    account_id: int
    role: str
    user_type: int
    phone: str
    username: str | None
    shop_id: int | None
    caps: list[str]
    note: str | None = None


# 已知測試帳號種子（id 與登入資料）。能力(caps)由 sync 探測，不寫死在這裡。
# labor：audit 打工夥伴；employer：audit 商家。
SEED_LABOR_IDS = [236, 276, 365, 15, 214, 373]
SEED_EMPLOYERS = [
    {"account_id": 129, "phone": "0923113000", "username": "886923113000", "shop_id": 70},
]
# 供給階段可校正硬狀態的測試帳號白名單（只動這些測試帳號，不波及真實用戶）。
PROVISION_LABOR_IDS = [236, 276, 365, 15, 214]


class AccountPool:
    """帳號池存取層。執行期(acquire/release)只碰 QA 庫；供給/同步才連工作庫。"""

    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def _qa_engine(self):
        return qa_models.get_engine(self.s)

    def _worky_engine(self):
        """連工作庫（僅供給/同步用；執行期不呼叫）。"""
        s = self.s
        url = f"mysql+pymysql://{s.db_user}:{s.db_pass}@{s.db_host}:{s.db_port}/{s.db_name}?charset=utf8mb4"
        return sqlalchemy.create_engine(url, future=True)

    # ── 執行期：配發 / 歸還（只讀寫 QA 庫）──────────────────────────────────────
    def acquire(self, role: str, caps_required: list[str], n: int, *,
                owner: str, lease_secs: int = 900, lease: bool = True) -> list[PooledAccount]:
        """配發 n 個 role 帳號，需具備 caps_required 全部能力；不足則 PoolShortage。

        lease=True：加軟租約（available 或租約過期者可被借，借走標 leased + 到期）。
        lease=False：純選取不上鎖（同步循序執行時用，避免反覆 run 互卡）。
        """
        now = int(time.time())
        want = set(caps_required)
        with self._qa_engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT id, account_id, role, user_type, phone, username, shop_id, caps, note
                FROM qa_accounts
                WHERE role=:role AND state<>'disabled'
                  AND (state='available' OR lease_expires_at < :now)
                ORDER BY (lease_owner=:owner) DESC, account_id ASC
            """), {"role": role, "now": now, "owner": owner}).all()
            def _caps(r) -> list[str]:
                c = r.caps
                return json.loads(c) if isinstance(c, str) else (c or [])

            chosen = []
            for r in rows:
                if want <= set(_caps(r)):
                    chosen.append(r)
                if len(chosen) >= n:
                    break
            if len(chosen) < n:
                avail = [(r.account_id, sorted(_caps(r))) for r in rows]
                raise PoolShortage(
                    f"role={role} 需要 {n} 個具備 {sorted(want)} 的帳號，僅 {len(chosen)} 個符合。"
                    f"\n可用候選(account_id, caps)={avail}"
                )
            if lease:
                exp = now + lease_secs
                for r in chosen:
                    conn.execute(text(
                        "UPDATE qa_accounts SET state='leased', lease_owner=:o, lease_expires_at=:e WHERE id=:id"
                    ), {"o": owner, "e": exp, "id": r.id})
        return [PooledAccount(
            account_id=r.account_id, role=r.role, user_type=r.user_type, phone=r.phone,
            username=r.username, shop_id=r.shop_id, caps=_caps(r), note=r.note,
        ) for r in chosen]

    def acquire_lacking(self, role: str, lacking: str, base_caps: list[str], n: int = 1, *,
                        owner: str, lease: bool = False) -> list[PooledAccount]:
        """配「具備 base_caps 但**缺** lacking 能力」的帳號（負向用例用）。

        後端驗證有序：要可靠觸發某個守衛失敗，帳號須只缺該目標能力、其餘前置都滿足，
        否則會先卡在更前面的檢查。base_caps 即「其餘必須具備」的能力。
        """
        now = int(time.time())
        base = set(base_caps)
        with self._qa_engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT id, account_id, role, user_type, phone, username, shop_id, caps, note
                FROM qa_accounts
                WHERE role=:role AND state<>'disabled'
                  AND (state='available' OR lease_expires_at < :now)
                ORDER BY account_id ASC
            """), {"role": role, "now": now}).all()

            def _caps(r):
                c = r.caps
                return set(json.loads(c) if isinstance(c, str) else (c or []))

            chosen = [r for r in rows if base <= _caps(r) and lacking not in _caps(r)][:n]
            if len(chosen) < n:
                avail = [(r.account_id, sorted(_caps(r))) for r in rows]
                raise PoolShortage(
                    f"role={role} 需要 {n} 個『具 {sorted(base)} 但缺 {lacking}』的帳號，僅 {len(chosen)} 個。"
                    f"\n候選={avail}"
                )
            if lease:
                exp = now + 900
                for r in chosen:
                    conn.execute(text(
                        "UPDATE qa_accounts SET state='leased', lease_owner=:o, lease_expires_at=:e WHERE id=:id"
                    ), {"o": owner, "e": exp, "id": r.id})
        return [PooledAccount(
            account_id=r.account_id, role=r.role, user_type=r.user_type, phone=r.phone,
            username=r.username, shop_id=r.shop_id,
            caps=list(json.loads(r.caps) if isinstance(r.caps, str) else (r.caps or [])), note=r.note,
        ) for r in chosen]

    def release(self, owner: str) -> int:
        """歸還某 owner 借走的所有帳號。"""
        with self._qa_engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_accounts SET state='available', lease_owner=NULL, lease_expires_at=0 "
                "WHERE lease_owner=:o"), {"o": owner})
            return res.rowcount

    def list_all(self) -> list[dict[str, Any]]:
        with self._qa_engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(
                "SELECT account_id, role, caps, state, note FROM qa_accounts ORDER BY role, account_id")).all()]

    # ── 供給 + 同步（特權、偶發；連工作庫）──────────────────────────────────────
    def provision(self, *, heal: bool = True) -> dict[str, Any]:
        """在有工作庫權限的環境跑：校正測試帳號硬狀態 + 探測能力 → 寫 qa_accounts。

        heal=True 時對 PROVISION_LABOR_IDS 做冪等校正：
          · 上架 audit role（s_labor_roles.published=1）
          · 清掉「測試殘留」的有效停權（labor_suspension）
        然後 sync_caps() 探測能力寫池。回傳摘要。
        """
        weng = self._worky_engine()
        healed: dict[str, int] = {}
        try:
            if heal:
                ids = tuple(PROVISION_LABOR_IDS)
                with weng.begin() as wc:
                    healed["audit_role_published"] = wc.execute(text(
                        "UPDATE s_labor_roles SET published=1 "
                        "WHERE labor_id IN :ids AND role_id=10 AND published<>1"
                    ), {"ids": ids}).rowcount
                    healed["suspension_cleared"] = wc.execute(text(
                        "DELETE FROM s_labor_suspension "
                        "WHERE labor_id IN :ids AND suspend_end_at > UNIX_TIMESTAMP()"
                    ), {"ids": ids}).rowcount
            synced = self.sync_caps(weng)
        finally:
            weng.dispose()
        return {"healed": healed, "synced": synced}

    def sync_caps(self, weng=None) -> int:
        """探測各種子帳號能力，upsert 進 qa_accounts（保留既有租約）。回傳同步筆數。"""
        own_engine = weng is None
        weng = weng or self._worky_engine()
        now = int(time.time())
        try:
            seeds: list[PooledAccount] = []
            with weng.connect() as wc:
                # ── labor ──
                for uid in SEED_LABOR_IDS:
                    r = wc.execute(text(
                        "SELECT id, phone, username, valid_status, is_profile_complete "
                        "FROM s_labors WHERE id=:i"), {"i": uid}).first()
                    if not r:
                        continue
                    published = wc.execute(text(
                        "SELECT 1 FROM s_labor_roles WHERE labor_id=:i AND role_id=10 AND published=1"
                    ), {"i": uid}).first() is not None
                    suspended = wc.execute(text(
                        "SELECT 1 FROM s_labor_suspension WHERE labor_id=:i "
                        "AND suspend_start_at<=:n AND suspend_end_at>:n LIMIT 1"
                    ), {"i": uid, "n": now}).first() is not None
                    penalty = wc.execute(text(
                        "SELECT COUNT(*) FROM s_labor_penalty_point_logs WHERE labor_id=:i"
                    ), {"i": uid}).scalar() or 0
                    caps = []
                    if r.valid_status == 1: caps.append("verified")
                    if r.is_profile_complete == 1: caps.append("profile_complete")
                    if published: caps.append("audit_role")
                    if not suspended: caps.append("active")
                    # clean = 無違規點數歷史。有違規的帳號(殘留停權/扣點)在 apply 會以泛用
                    # 10001 失敗，DB 欄位看不出來，故獨立成一個能力讓申請者用例避開。
                    if penalty == 0: caps.append("clean")
                    notes = []
                    if suspended: notes.append("suspended")
                    if penalty: notes.append(f"penalty_logs={penalty}")
                    seeds.append(PooledAccount(
                        account_id=uid, role="labor", user_type=2, phone=r.phone or "",
                        username=r.username, shop_id=None, caps=caps,
                        note=";".join(notes) or None))
                # ── employer（商家）──
                for emp in SEED_EMPLOYERS:
                    uid = emp["account_id"]
                    e = wc.execute(text("SELECT id FROM s_employers WHERE id=:i"), {"i": uid}).first()
                    caps = ["active"]
                    shop = wc.execute(text(
                        "SELECT validation_type FROM s_shops WHERE id=:s"), {"s": emp["shop_id"]}).first()
                    if shop and shop.validation_type == 2:
                        caps.append("verified_shop")
                    seeds.append(PooledAccount(
                        account_id=uid, role="employer", user_type=1, phone=emp["phone"],
                        username=emp["username"], shop_id=emp["shop_id"], caps=caps,
                        note=None if e else "missing_in_worky"))

            with self._qa_engine.begin() as qc:
                for a in seeds:
                    qc.execute(text("""
                        INSERT INTO qa_accounts
                          (account_id, role, user_type, phone, username, shop_id, caps, state, note, synced_at)
                        VALUES (:account_id, :role, :user_type, :phone, :username, :shop_id, :caps, 'available', :note, :now)
                        ON DUPLICATE KEY UPDATE
                          user_type=VALUES(user_type), phone=VALUES(phone), username=VALUES(username),
                          shop_id=VALUES(shop_id), caps=VALUES(caps), note=VALUES(note), synced_at=VALUES(synced_at)
                    """), {
                        "account_id": a.account_id, "role": a.role, "user_type": a.user_type,
                        "phone": a.phone, "username": a.username, "shop_id": a.shop_id,
                        "caps": json.dumps(a.caps), "note": a.note, "now": now,
                    })
            return len(seeds)
        finally:
            if own_engine:
                weng.dispose()


def main(argv: list[str] | None = None) -> int:
    """CLI：帳號池供給/同步/檢視。

        python -m worky_regression.qa_accounts provision   # 校正硬狀態 + 同步能力（特權）
        python -m worky_regression.qa_accounts sync         # 只同步能力，不校正
        python -m worky_regression.qa_accounts list         # 檢視池現況
    """
    import argparse

    ap = argparse.ArgumentParser(prog="worky-qa-accounts")
    ap.add_argument("cmd", choices=["provision", "sync", "list"], default="list", nargs="?")
    ap.add_argument("--no-heal", action="store_true", help="provision 時不校正硬狀態")
    args = ap.parse_args(argv)

    pool = AccountPool(Settings.from_env())
    if args.cmd == "provision":
        print(pool.provision(heal=not args.no_heal))
    elif args.cmd == "sync":
        print({"synced": pool.sync_caps()})
    for a in pool.list_all():
        print(f"  {a['role']:8} {a['account_id']:>5}  {a['state']:9} {a['caps']}  {a['note'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
