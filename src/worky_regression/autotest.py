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
import time
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


def ensure_employer_invoice(actor: Actor) -> None:
    """確保 job 商家已設定發票資訊，否則非企業支付的 /employer/shop/job/publish 會 20017。

    呼叫 /employer/invoice/update 寫入最小設定（type=0 捐贈發票）。Idempotent。
    API 自建的池商家沒有發票資訊（種子帳號 129 是手工設的），配發後一律補一次。
    """
    resp = actor.client.post(
        "/employer/invoice/update",
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
            f"failed to setup invoice for employer id={actor.user_id}: {resp.text[:300]}"
        )


def _job_history(s: Settings) -> dict:
    """今天（本地日界）的 job 配對史；看板庫不可用時回空史（退化為純 LRU 配發）。"""
    lt = time.localtime()
    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    try:
        return QAStore(s).job_allocation_history("job", day0)
    except Exception as e:  # noqa: BLE001 — 史料只是避撞優化，讀不到不可擋執行
        print(f"  [actors] 讀今日配對史失敗（退化為純 LRU 配發）：{e}")
        return {"accepted_pairs": set(), "occupied": {}, "publish": {}}


def _pick_job_pair(s: Settings, pool: AccountPool, hist: dict, labor_caps: list[str],
                   exclude: dict[str, list[str]]) -> tuple[Actor, list[Actor]]:
    """選一個商家 + 2 個夥伴，避開後端「夥伴×商家×日」類限制。

    依配對史避讓（規則 → 對應錯誤碼）：
    - 硬避：今日已有 J3 錄取的 (商家, 夥伴) 配對——同一企業/商家每日僅限工作一次
      （30229 LABOR_THAT_DAY_MATCH_JOB_SUCCESS_ONLINE / 30213 LABOR_HAS_JOB_ON_THE_SAME_DAY_AT_THE_SHOP），
      連帶覆蓋勞基法同企業連續上工/兩班間隔（30227/30228/30230、20322-20324）。
    - 硬避：時段佔用中的夥伴（30207 該時段已有確認工作）——錄取即佔住表定時段直到
      end_at，跨商家、run 死掉沒人打卡也一樣佔用，放寬必撞，所以不做軟回退。
    - 軟避：600s 內發佈過的商家（dev min_publish_interval / 40418；其他商家都不行才用，
      順帶分散 20009 單店單日發佈上限）。
    全部商家的可配夥伴都耗盡 → PoolShortage（列出已燒配對與佔用截止，提示擴池或晚點再跑）。
    """
    now = int(time.time())
    ex_lab = list(exclude.get("labor") or [])
    tried: list[str] = []
    deferred: list[Actor] = []   # 僅因 600s 發佈間隔被後置的商家
    while True:
        try:
            emp = _login_from_pool(s, pool, "employer", ["active", "shop_approved"], 1,
                                   owner="job-actors",
                                   exclude=(exclude.get("employer") or []) + tried)[0]
        except PoolShortage:
            break
        tried.append(str(emp.user_id))
        pub = hist["publish"].get(str(emp.user_id)) or {}
        if now - int(pub.get("last_at") or 0) < 600:
            deferred.append(emp)
            continue
        la = _labors_for_employer(s, pool, emp, hist, labor_caps, ex_lab)
        if la is not None:
            return emp, la
    for emp in deferred:
        la = _labors_for_employer(s, pool, emp, hist, labor_caps, ex_lab)
        if la is not None:
            print(f"  [actors] 商家 #{emp.user_id} 距上次發佈 <600s，可能撞發佈間隔（無其他可用商家）")
            return emp, la
    now2 = int(time.time())
    burned = sorted(hist["accepted_pairs"])
    busy = {l: time.strftime("%H:%M", time.localtime(ts))
            for l, ts in hist["occupied"].items() if ts > now2}
    raise PoolShortage(
        "今日 (商家×夥伴) 配對額度耗盡：同一企業/商家每日僅限工作一次（30229/30213）"
        f"＋時段佔用（30207）。今日已錄取配對(employer,labor)={burned}；"
        f"時段佔用中(labor:至)={busy}。"
        "請擴池（看板「帳號池」頁註冊+審核新夥伴/商家），或等佔用過期再跑。")


def _labor_slot_conflict(actor: Actor, window: tuple[int, int]) -> bool:
    """30207 preflight：以夥伴身份查媒合清單，檢查是否有「已確認」工作與目標時段重疊。

    史料推算的佔用（job_allocation_history.occupied）對「actors 已丟失的舊中斷 run」
    與「框架之外的手動佔用」是盲的；這裡直接問後端，status=6（商家錄取）的工作佔住
    表定 start_at~end_at。查詢失敗保守視為不衝突（後端在 J2 仍是最終裁決）。
    """
    try:
        r = actor.client.get("/labor/job-match/list", params={"type": 3})
        items = ((r.json().get("data") or {}).get("matching")) or []
    except Exception:  # noqa: BLE001
        return False
    s0, s1 = window
    return any(int(it.get("status", 0)) == 6
               and int(it.get("start_at", 0)) < s1 and int(it.get("end_at", 0)) > s0
               for it in items)


def _acquire_clear_labors(s: Settings, pool: AccountPool, n: int, labor_caps: list[str],
                          excluded: list[str]) -> list[Actor]:
    """配 n 個「目標時段乾淨」的夥伴：每配一個就 preflight 時段衝突，髒的排除再補。

    目標時段取保守包絡 [now+11min, now+13min+130min]（新工作 start≈now+13min、
    工時上限 120min + 緩衝）。excluded 會被就地擴充（呼叫端可繼續沿用）。
    """
    now = int(time.time())
    window = (now + 11 * 60, now + (13 + 120 + 10) * 60)
    picked: list[Actor] = []
    while len(picked) < n:
        cands = _login_from_pool(s, pool, "labor", labor_caps, n - len(picked),
                                 owner="job-actors", exclude=excluded)
        for a in cands:
            excluded.append(str(a.user_id))
            if _labor_slot_conflict(a, window):
                print(f"  [actors] 夥伴 #{a.user_id} 時段被確認工作佔用（30207 preflight），換下一個")
                continue
            picked.append(a)
    return picked


def _labors_for_employer(s: Settings, pool: AccountPool, emp: Actor, hist: dict,
                         labor_caps: list[str], ex_lab: list[str]) -> list[Actor] | None:
    """為指定商家配 2 個夥伴：硬排「今日已錄取配對」與「時段佔用中」，配到後再
    逐一 preflight 時段衝突（API 實查，補史料盲區）；不足回 None。

    佔用（30207）是跨商家硬約束（被錄取的工作佔住表定時段直到 end_at），放寬必撞，
    不做軟回退。新工作 start ≈ now+13min，故佔用截止 > now+600s 即視為衝突。
    """
    now = int(time.time())
    burned = [l for (e, l) in hist["accepted_pairs"] if e == str(emp.user_id)]
    busy = [l for (l, ts) in hist["occupied"].items() if ts > now + 600]
    try:
        return _acquire_clear_labors(s, pool, 2, labor_caps, ex_lab + burned + busy)
    except PoolShortage:
        return None


def required_actors(spec: dict) -> set[str]:
    """掃出 path spec 實際引用的具名 actor（bind 重綁目標、api 查驗指定的 actor）。

    transition 自身的 actor_role / pushes_to 是基礎角色（labor / employer / publisher…），
    _actors_for 必配；這裡只收「配發端視為選配、可能缺席」的具名引用（labor3、
    labor_lacking_* 等），讓 _actors_for 在執行前就知道哪些選配 actor 其實是硬需求。
    """
    names: set[str] = set()
    for step in spec.get("path") or []:
        if not isinstance(step, dict):
            continue
        for src in (step.get("bind") or {}).values():
            names.add(str(src))
        for key in ("assert_api", "wait_api"):
            sub = step.get(key)
            if isinstance(sub, dict) and sub.get("actor"):
                names.add(str(sub["actor"]))
        api = (step.get("expect") or {}).get("api")
        if isinstance(api, dict) and api.get("actor"):
            names.add(str(api["actor"]))
    return names


def _actors_for(system: str, s: Settings,
                exclude: dict[str, list[str]] | None = None,
                required: set[str] | None = None) -> dict[str, Actor]:
    """依系統登入對應角色（承攬制 publisher 會做發票 preflight）。

    exclude：{role: [account_id,...]}，配發時跳過這些帳號（「換一個號」用，
    只對池配發的 job/activity 角色有效；contract 為固定 audit 帳號不受影響）。
    required：用例引用的具名 actor（required_actors(spec)）。選配 actor（labor3 /
    labor_lacking_*）若在其中即升級為硬需求：配不到立刻拋 PoolShortage，在發佈任何
    工作前就失敗（避免跑到中途 KeyError，白燒一次發佈間隔與當日配對）。
    """
    accounts = yaml.safe_load(ACCOUNTS.read_text(encoding="utf-8"))
    exclude = exclude or {}
    required = required or set()

    def _check(actors: dict[str, Actor]) -> dict[str, Actor]:
        missing = sorted(n for n in required if n not in actors)
        if missing:
            raise PoolShortage(
                f"用例引用的 actor 未能配發：{missing}（已配：{sorted(actors)}）。"
                f"若名稱拼錯請改用例 YAML；若是池不足請補帳號（register/provision）。")
        return actors
    if system == "job":
        # 從帳號池按「能力」配發，不再寫死 id：labor1/2/3 為三個合格夥伴（bind 切換身份），
        # employer 為店鋪已過審商家。執行期只讀 qa_accounts，不直連工作庫挖帳號。
        pool = AccountPool(s)
        # 不要求 audit_role：role 10（AUDIT_USER）在後端只閘「固定審核碼登入」
        # （LoginConfirmForm::isAuditUser），apply/錄取/打卡/評價全不檢查；框架登入統一走
        # dev 發碼（Actor.login 不用固定碼）。要求它會把合格夥伴鎖死在 provision 種子帳號，
        # 打卡類用例時段釘在「現在+N分」，同兩個號短時間重跑必撞 30207（該時段已有確認工作）。
        labor_caps = ["verified", "profile_complete", "active", "clean"]
        # 配對感知配發（#717 30229）：單帳號 LRU 看不見「夥伴×商家」配對維度，小池會踩回
        # 當日燒過的組合。從今日 qa_runs 還原配對史，硬避已錄取配對、軟避時段衝突與發佈間隔；
        # 規則對應的後端錯誤碼見 _pick_job_pair docstring。
        # employer 要 shop_approved（店鋪 validation_status=3 已過審）才能發佈工作；
        # verified_shop 只代表「公司型已送審」，未過審發佈會 20022（JobForm::validateCanCreateJob）。
        hist = _job_history(s)
        emp, la = _pick_job_pair(s, pool, hist, labor_caps, exclude)
        ensure_employer_invoice(emp)   # 無發票資訊發佈會 20017（非企業支付都檢查）
        actors = {
            "employer": emp,
            "labor": la[0],
            "labor1": la[0],
            "labor2": la[1],
        }
        # 第三個合格夥伴：排除已用兩位與本商家燒過的配對，再配一個（同樣
        # preflight 時段衝突——labor3 也會申請，J2 一樣被 30207 擋）。
        # 用例有引用（required）時是硬需求，配不到立即失敗；否則選配、池不足略過。
        try:
            used = [str(a.user_id) for a in la]
            burned3 = [l for (e, l) in hist["accepted_pairs"] if e == str(emp.user_id)]
            actors["labor3"] = _acquire_clear_labors(
                s, pool, 1, labor_caps,
                (exclude.get("labor") or []) + used + burned3)[0]
        except PoolShortage as e:
            if "labor3" in required:
                raise PoolShortage(
                    f"用例需要第三位合格夥伴 labor3，但排除本次已用 {used}、本商家當日已錄取"
                    f"配對與時段佔用後，池中已無可配帳號：{e}\n"
                    f"建議：register/provision 補合格 labor 入池，或晚點再跑（佔用釋放）。") from e
        # 負向用例用的「缺能力」夥伴（deficiency actor）：用例有引用時硬要，否則盡力配發。
        # 帳號須「只缺目標能力、其餘前置滿足」才能可靠觸發對應守衛失敗（後端驗證有序）。
        deficiency = {
            "labor_lacking_verified": ("verified", ["profile_complete", "active", "clean"]),
            "labor_lacking_profile_complete": ("profile_complete", ["active"]),
        }
        for name, (lack, base) in deficiency.items():
            try:
                pa = pool.acquire_lacking("labor", lack, base, owner="job-actors", lease=False)[0]
                actors[name] = _actor_from_pool(s, pa, "labor", pool)
            except Exception as e:  # noqa: BLE001 — 池中無此缺能力帳號就不提供，產生器會跳過對應分支
                if name in required:
                    raise PoolShortage(
                        f"用例需要缺能力夥伴 {name}（缺 {lack}、具 {base}），但池中無此帳號：{e}\n"
                        f"建議：provision 一個對應缺能力帳號入池。") from e
        return _check(actors)
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
            if "employer" in required:
                raise
        return _check(actors)
    publisher = _build_actor(s, accounts, "publisher", "publisher_primary", 2)
    ensure_publisher_invoice(publisher)
    return _check({
        "publisher": publisher,
        "receiver": _build_actor(s, accounts, "receiver", "receiver_primary", 2),
    })


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
    actors = _actors_for(system, s, required=required_actors(spec))
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
