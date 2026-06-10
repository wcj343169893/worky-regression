"""Layer ③ CLI — 後台跑「用例 → 任務流 → 執行 → 記錄結果」整條管線。

    # 用自然語言用例（需 DEEPSEEK_API_KEY）
    python -m worky_regression.autotest "商家發工作，夥伴申請後商家取消錄取"

    # 只分解、不執行（看產出的任務流 YAML）
    python -m worky_regression.autotest "..." --dry-run

    # 跳過分解，直接跑手寫/先前產出的 lean plan
    python -m worky_regression.autotest --plan plan.json

    # 直接跑既有的 path YAML（不分解）
    python -m worky_regression.autotest --path cases/job-happy-core.yaml

產出：cases/generated/<path_id>.yaml（產生的任務流，可檢視/編輯）
      results/<path_id>-<ts>.json（執行結果記錄）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .actor import Actor, LoginFailedError
from .client import WorkyClient
from .config import Settings
from .planner import TaskPlan, build_path, decompose, plan_to_json
from .qa_accounts import AccountPool, PoolShortage, PooledAccount
from .qa_store import QAStore
from .recorder import RecordingRunner
from .verifier import DBVerifier

ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS = ROOT / "cases" / "_fixtures" / "test_accounts.yaml"
GENERATED = ROOT / "cases" / "generated"


def _build_actor(s: Settings, accounts: dict, role: str, key: str, user_type: int) -> Actor:
    cfg = accounts[key]
    client = WorkyClient(s, user_type=user_type)
    actor = Actor(role=role, user_type=user_type, phone=cfg["phone"],
                  user_id=cfg["id"], client=client, shop_id=cfg.get("shop_id"))
    actor.login()
    return actor


def _actor_from_pool(s: Settings, pa: PooledAccount, role: str,
                     pool: AccountPool | None = None) -> Actor:
    """用帳號池配發的帳號取得已認證 Actor（執行期只認 caps，不認 id）。

    token 走「有效就用、到期才刷」：
      ① 池內 access token 仍有效（含 5 分鐘緩衝）→ 直接用，不打網路。
      ② access 過期但 refresh 仍有效 → POST /token/refresh 換新 access token。
      ③ refresh 也過期 / 無 token / 刷新被拒 → 完整登入。
    任何拿到新 token 的路徑都把結果寫回池（save_token），下次配發重用。
    """
    client = WorkyClient(s, user_type=pa.user_type)
    actor = Actor(role=role, user_type=pa.user_type, phone=pa.phone,
                  user_id=pa.account_id, client=client, shop_id=pa.shop_id)

    cached = pool.load_token(pa.account_id, role) if pool else None
    if cached and cached.get("access_token"):
        client.set_access_token(
            token=cached["access_token"],
            expired_at=int(cached.get("access_token_expired_at") or 0),
            refresh_token=cached.get("refresh_token") or "",
            refresh_expired_at=int(cached.get("refresh_token_expired_at") or 0),
        )

    if client.access_valid():
        actor._logged_in = True            # ① 池內 token 仍有效，免登入
    elif client.refresh_valid() and client.refresh():
        actor._logged_in = True            # ② 刷新成功
        if pool:
            pool.save_token(pa.account_id, role, access_token=client.access_token,
                            refresh_token=client.refresh_token,
                            access_expired_at=client.access_token_expired_at,
                            refresh_expired_at=client.refresh_token_expired_at)
    else:
        actor.login()                             # ③ 完整登入：發碼→確認（不用固定碼）
        if pool:
            pool.save_token(pa.account_id, role, access_token=client.access_token,
                            refresh_token=client.refresh_token,
                            access_expired_at=client.access_token_expired_at,
                            refresh_expired_at=client.refresh_token_expired_at)
    return actor


def _login_from_pool(s: Settings, pool: AccountPool, role: str, caps: list[str], n: int,
                     *, owner: str, exclude: list[str] | None = None) -> list[Actor]:
    """配發並登入 n 個 role 帳號；登入失敗者自動排除、改配池中下一個同能力帳號（#2 自動換號）。

    actor 登入失敗（如 audit 登入碼被拒 40003、帳號被鎖）不再讓整支用例直接死，而是排除該號、
    重配同 caps 的替補，直到湊滿 n。替補耗盡（剩餘合格帳號不足）才拋 PoolShortage——此時是
    「池內可登入的合格帳號不夠」，訊息明確，提示補帳號（見帳號池管理頁）。
    """
    excluded = [str(x) for x in (exclude or [])]
    actors: list[Actor] = []
    while len(actors) < n:
        # acquire 不足 n-len 時自會拋 PoolShortage（替補已耗盡）→ 直接往外傳
        pas = pool.acquire(role, caps, n - len(actors), owner=owner, lease=False, exclude=excluded)
        for pa in pas:
            # 不論登入成敗都排除此號，避免 lease=False 下次輪重配到同一個（造成重複身分）
            excluded.append(str(pa.account_id))
            try:
                actors.append(_actor_from_pool(s, pa, role, pool))
            except LoginFailedError as e:
                print(f"  [actors] {role} #{pa.account_id} 登入失敗，自動換號：{e}")
    return actors


def ensure_publisher_invoice(actor: Actor) -> None:
    """確保承攬制 publisher 已設定發票資訊，否則 /contract/task/publish 會 throw 50045。

    以 audit publisher 身份呼叫 /contract/invoice/update 寫入最小設定（type=0 捐贈發票）。
    Idempotent：覆寫舊值不會壞事。conftest 與 autotest/dashboard 共用此 preflight。
    """
    resp = actor.client.post(
        "/contract/invoice/update",
        body={
            "type": 0,                          # 捐贈發票
            "name": "regression",
            "phone": actor.phone,
            "email": "regression@worky.local",
            "e_invoice_carrier_type": 0,        # 無載具（捐贈用）
            "mobile_carrier_number": "",
            "citizen_carrier_number": "",
            "tax_id_number": "",
            "tax_id_number_title": "",
        },
    )
    if resp.status_code != 200 or resp.json().get("success") is False:
        raise RuntimeError(
            f"failed to setup invoice for publisher id={actor.user_id}: {resp.text[:300]}"
        )


def _actors_for(system: str, s: Settings,
                exclude: dict[str, list[str]] | None = None) -> dict[str, Actor]:
    """依系統登入對應角色（承攬制 publisher 會做發票 preflight）。

    exclude：{role: [account_id,...]}，配發時跳過這些帳號（「換一個號」用，
    只對池配發的 job/activity 角色有效；contract 為固定 audit 帳號不受影響）。
    """
    accounts = yaml.safe_load(ACCOUNTS.read_text(encoding="utf-8"))
    exclude = exclude or {}
    if system == "job":
        # 從帳號池按「能力」配發，不再寫死 id：labor1/2/3 為三個合格夥伴（bind 切換身份），
        # employer 為已驗證店鋪商家。執行期只讀 qa_accounts，不直連工作庫挖帳號。
        pool = AccountPool(s)
        labor_caps = ["verified", "profile_complete", "audit_role", "active", "clean"]
        # 登入失敗自動換號（#2）：配 2 個「可登入」的全 caps 夥伴 + 1 個可登入的商家。
        la = _login_from_pool(s, pool, "labor", labor_caps, 2, owner="job-actors",
                              exclude=exclude.get("labor"))
        emp = _login_from_pool(s, pool, "employer", ["active", "verified_shop"], 1,
                               owner="job-actors", exclude=exclude.get("employer"))[0]
        actors = {
            "employer": emp,
            "labor": la[0],
            "labor1": la[0],
            "labor2": la[1],
        }
        # 第三個合格夥伴（選配）：排除已用兩位，再配一個可登入全 caps 夥伴；池不足就略過。
        try:
            used = [str(a.user_id) for a in la]
            actors["labor3"] = _login_from_pool(
                s, pool, "labor", labor_caps, 1, owner="job-actors",
                exclude=(exclude.get("labor") or []) + used)[0]
        except PoolShortage:
            pass
        # 負向用例用的「缺能力」夥伴（deficiency actor）：盡力配發，池中沒有就略過該名。
        # 帳號須「只缺目標能力、其餘前置滿足」才能可靠觸發對應守衛失敗（後端驗證有序）。
        deficiency = {
            "labor_lacking_verified": ("verified", ["profile_complete", "active", "clean", "audit_role"]),
            "labor_lacking_profile_complete": ("profile_complete", ["active", "audit_role"]),
        }
        for name, (lack, base) in deficiency.items():
            try:
                pa = pool.acquire_lacking("labor", lack, base, owner="job-actors", lease=False)[0]
                actors[name] = _actor_from_pool(s, pa, "labor", pool)
            except Exception:  # noqa: BLE001 — 池中無此缺能力帳號就不提供，產生器會跳過對應分支
                pass
        return actors
    if system == "activity":
        # 營運活動（Activity API）唯讀查詢：打工端用 labor token、商家端用 employer token。
        # 登入失敗自動換號（#2）；湊不到才 PoolShortage。
        pool = AccountPool(s)
        actors = {"labor": _login_from_pool(s, pool, "labor", ["audit_role", "active"], 1,
                                            owner="activity-actors", exclude=exclude.get("labor"))[0]}
        # 商家端活動端點（/employer/...）需要 employer；池中有可登入的就配，沒有則略過該角色
        try:
            actors["employer"] = _login_from_pool(s, pool, "employer", ["active", "verified_shop"], 1,
                                                  owner="activity-actors", exclude=exclude.get("employer"))[0]
        except PoolShortage:  # 無合格/可登入商家帳號時，僅打工端活動可測
            pass
        return actors
    publisher = _build_actor(s, accounts, "publisher", "publisher_primary", 2)
    ensure_publisher_invoice(publisher)
    return {
        "publisher": publisher,
        "receiver": _build_actor(s, accounts, "receiver", "receiver_primary", 2),
    }


def _load_plan(path: Path) -> TaskPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    return TaskPlan(path_id=data["path_id"], description=data["description"],
                    system=data["system"], steps=data["steps"], raw=data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worky-autotest", description="用例 → 任務流 → 執行 → 記錄")
    ap.add_argument("use_case", nargs="?", help="自然語言用例")
    ap.add_argument("--plan", type=Path, help="跳過分解，讀 lean plan JSON")
    ap.add_argument("--path", type=Path, help="直接跑既有 path YAML（不分解）")
    ap.add_argument("--dry-run", action="store_true", help="只產生任務流，不執行")
    ap.add_argument("--no-save", action="store_true", help="不寫 cases/generated/")
    args = ap.parse_args(argv)

    s = Settings.from_env()

    # 1) 取得 path dict（三種來源）
    if args.path:
        spec = yaml.safe_load(args.path.read_text(encoding="utf-8"))
        system = "job" if any("J" == str(st.get("transition", "?"))[:1]
                              for st in spec["path"]) else "contract"
        print(f"▶ 跑既有 path：{args.path}")
    else:
        if args.plan:
            plan = _load_plan(args.plan)
            print(f"▶ 讀 lean plan：{args.plan}")
        elif args.use_case:
            print(f"▶ 分解用例：{args.use_case}")
            plan = decompose(args.use_case, s)
        else:
            ap.error("需要 use_case，或 --plan，或 --path 之一")
            return 2
        print(f"  系統={plan.system}  path_id={plan.path_id}")
        print(f"  描述={plan.description}")
        for i, st in enumerate(plan.steps):
            label = st.get("transition") or f"db_exec: {st.get('sql', '')[:50]}"
            note = f"  # {st['note']}" if st.get("note") else ""
            print(f"   {i}. [{st['kind']}] {label}{note}")
        spec = build_path(plan)
        system = plan.system

    # 2) 落地產生的任務流 YAML（可檢視/編輯/重跑）
    if not args.path and not args.no_save:
        GENERATED.mkdir(parents=True, exist_ok=True)
        out = GENERATED / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"  ↳ 任務流已寫入 {out}")

    if args.dry_run:
        print("\n--- 產生的 path（--dry-run，未執行）---")
        print(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
        return 0

    # 3) 登入角色 + 執行 + 記錄（contract 與 job 在 dev 分庫，依系統選 DB）
    db = DBVerifier(s.for_system(system))
    actors = _actors_for(system, s)
    qa = QAStore(s)
    qa.migrate()
    result = RecordingRunner(db, qa_store=qa, system=system).run(spec, actors=actors)

    print(f"\n{'='*60}")
    print(result.summary())
    for st in result.steps:
        mark = {"passed": "✓", "failed": "✗", "skipped": "·"}.get(st.status, "?")
        extra = ""
        if st.observations.get("saved"):
            extra = f"  saved={st.observations['saved']}"
        elif st.error:
            extra = f"  {st.error}"
        print(f"  {mark} [{st.index}] {st.name:30s} {st.elapsed_ms:>5}ms{extra}")
    print(f"{'='*60}")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
