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
import re
import sys
import threading
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
GENERATED = ROOT / "cases" / "generated"

# job 系統「合格打工夥伴」的池配發能力（_actors_for 與自動換號 job_actor_swapper 共用；
# 為何不要求 audit_role 見 _actors_for 內註解）
JOB_LABOR_CAPS = ["verified", "profile_complete", "active", "clean"]

# 承攬制 publisher / receiver 的池配發能力：兩角色都是 Labor（user_type=2，
# /contract/* 全繼承 LaborApiController）。publisher 必須 verified——發佈任務後端
# 檢查實名認證（50024 發案者身份未通過認證，實測無 verified 帳號必撞）；receiver
# 不要求 verified（長期固定 receiver 276 無 verified 也全綠）。兩者都不要求
# audit_role（audit 只閘固定碼登入，框架走 dev 發碼，理由同 job）。
# publisher 配後另做發票 preflight（無發票發佈會 50045）。
CONTRACT_PUBLISHER_CAPS = ["verified", "profile_complete", "active", "clean"]
CONTRACT_RECEIVER_CAPS = ["profile_complete", "active", "clean"]


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


def actors_from_snapshot(s: Settings, system: str, snapshot: dict[str, dict],
                         *, owner: str, lease_secs: int = 1800
                         ) -> tuple[dict[str, Actor], AccountPool]:
    """依「掛起時落地的帳號快照」重建同一批已登入 Actor（resume_worker 喚醒續跑用）。

    長延時 run 掛起前已釋放租約（不能抱 24h），喚醒時必須**重新拿回同一批帳號**——它們
    早已綁定該 job_sn（已申請/錄取），不能換人。流程：按快照的 account_id 重上租約（owner
    為本次喚醒），再走 _actor_from_pool 登入（池 token 快取「有效就用、到期才刷」，吸收
    24h 後 access token 早已過期的情形）。回傳 (actors, pool)；呼叫端跑完須 pool.release(owner)。

    snapshot 形如 {role: {phone, user_id, user_type, shop_id, ...}}，role 含 employer /
    labor / labor1 / labor2…；labor 與 labor1 可能是同一人（同 account_id），登入只做一次
    再共用同一個 Actor（與正常配發 ``"labor": la[0], "labor1": la[0]`` 的別名語義一致）。
    """
    ssys = s.for_system(system)
    pool = AccountPool(ssys)
    ids = sorted({str(v.get("user_id")) for v in snapshot.values() if v.get("user_id")})
    if ids:
        pool.lease_accounts(ids, owner=owner, lease_secs=lease_secs)
    cache: dict[tuple[int, str], Actor] = {}
    actors: dict[str, Actor] = {}
    for key, info in snapshot.items():
        uid = info.get("user_id")
        if not uid:
            continue
        prole = "employer" if str(key).lower().startswith("employer") else "labor"
        ck = (int(uid), prole)
        if ck not in cache:
            pa = PooledAccount(
                account_id=int(uid), role=prole,
                user_type=int(info.get("user_type") or (1 if prole == "employer" else 2)),
                phone=info.get("phone") or "", username=None,
                shop_id=info.get("shop_id"), caps=[])
            cache[ck] = _actor_from_pool(ssys, pa, key, pool)
        actors[key] = cache[ck]
    return actors, pool


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


def _job_work_date(window: tuple[int, int]) -> str:
    """新工作的「工作日」(YYYY-MM-DD)，由時段包絡起點推算（30229/30213 比對基準）。"""
    return time.strftime("%Y-%m-%d", time.localtime(window[0]))


def _burned_labors(hist: dict, emp_id: str, work_date: str) -> list[str]:
    """該商家在「同一工作日」已 J3 錄取的夥伴——30229/30213 同企業同日僅一次。

    來源配對的 work_date 未知（舊用例無 vars 推不出工作日）時保守視為衝突，
    避免把無法判定的舊配對誤放（後端 J2 仍是最終裁決，撞了 swapper 會換）。
    """
    return [l for (e, l, d) in hist["accepted_pairs"]
            if e == emp_id and (d is None or d == work_date)]


def _busy_labors(hist: dict, window: tuple[int, int]) -> list[str]:
    """時段與新工作包絡「實際重疊」的夥伴——30207 該時段已有確認工作。

    取代舊的「有未過期佔用就避」：明天 11 點的佔用不該擋掉明天 13 點的新工作。
    """
    ns, ne = window
    return [l for l, wins in hist["occupied"].items()
            if any(s < ne and ns < e for (s, e) in wins)]


def _pick_job_pair(s: Settings, pool: AccountPool, hist: dict, labor_caps: list[str],
                   exclude: dict[str, list[str]],
                   window: tuple[int, int] | None = None) -> tuple[Actor, list[Actor]]:
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
        la = _labors_for_employer(s, pool, emp, hist, labor_caps, ex_lab, window)
        if la is not None:
            return emp, la
    for emp in deferred:
        la = _labors_for_employer(s, pool, emp, hist, labor_caps, ex_lab, window)
        if la is not None:
            print(f"  [actors] 商家 #{emp.user_id} 距上次發佈 <600s，可能撞發佈間隔（無其他可用商家）")
            return emp, la
    now2 = int(time.time())
    work_date = _job_work_date(window) if window else "—"
    burned = sorted((e, l) for (e, l, d) in hist["accepted_pairs"]
                    if d is None or d == work_date)
    busy = {l: "/".join(time.strftime("%m-%d %H:%M", time.localtime(s))
                        for s, e in wins if e > now2)
            for l, wins in hist["occupied"].items() if any(e > now2 for s, e in wins)}
    raise PoolShortage(
        f"工作日 {work_date} 的 (商家×夥伴) 配對額度耗盡：同一企業/商家每日僅限工作一次"
        "（30229/30213）＋時段佔用（30207）。"
        f"該工作日已錄取配對(employer,labor)={burned}；時段佔用中(labor:起)={busy}。"
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


def _slot_window(case_vars: dict | None) -> tuple[int, int]:
    """用例目標時段的保守包絡（30207 preflight 用）。

    有 vars.job_start_after_minutes 時以實際偏移＋工時推算（前後加緩衝）——
    「明天開工」類用例（after=1440）的佔用在明天，寫死 now+13min 包絡會全盲；
    無 vars 沿用預設包絡 [now+11min, now+13min+130min]（start≈now+13min、工時上限 120min）。
    """
    now = int(time.time())
    v = case_vars or {}
    after = v.get("job_start_after_minutes")
    if after is None:
        return (now + 11 * 60, now + (13 + 120 + 10) * 60)
    work = int(v.get("job_work_minutes", 120))
    return (now + (int(after) - 2) * 60, now + (int(after) + work + 10) * 60)


def _acquire_clear_labors(s: Settings, pool: AccountPool, n: int, labor_caps: list[str],
                          excluded: list[str],
                          window: tuple[int, int] | None = None) -> list[Actor]:
    """配 n 個「目標時段乾淨」的夥伴：每配一個就 preflight 時段衝突，髒的排除再補。

    window 為用例目標時段包絡（_slot_window；不給時用預設近時段包絡）。
    excluded 會被就地擴充（呼叫端可繼續沿用）。
    """
    window = window or _slot_window(None)
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
                         labor_caps: list[str], ex_lab: list[str],
                         window: tuple[int, int] | None = None) -> list[Actor] | None:
    """為指定商家配 2 個夥伴：硬排「同工作日已錄取配對」與「時段重疊佔用」，配到後再
    逐一 preflight 時段衝突（API 實查，補史料盲區）；不足回 None。

    佔用（30207）是跨商家硬約束（被錄取的工作佔住表定時段），放寬必撞，不做軟回退——
    但只避「時段實際重疊」者：明天 11 點的佔用不擋明天 13 點的新工作。
    """
    window = window or _slot_window(None)
    burned = _burned_labors(hist, str(emp.user_id), _job_work_date(window))
    busy = _busy_labors(hist, window)
    try:
        return _acquire_clear_labors(s, pool, 2, labor_caps, ex_lab + burned + busy, window)
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


# 配發互斥鎖：並行 run（看板批量執行）同時配發時，候選迭代（lease=False 不鎖號）
# 會看到相同的池快照而挑到同一組帳號。配發全程串行化＋配齊後整組上租約
# （lease_accounts），後續 run 的 acquire 自然跳過已租帳號 → 並行 run 帳號互斥。
# 配發只佔數秒（登入＋preflight），串行化不影響整體並行收益。
_ACTOR_ALLOC_LOCK = threading.Lock()


def _actors_for(system: str, s: Settings,
                exclude: dict[str, list[str]] | None = None,
                required: set[str] | None = None,
                case_vars: dict | None = None,
                lease_owner: str | None = None) -> dict[str, Actor]:
    """依系統登入對應角色（_actors_for_unlocked 的加鎖＋租約包裝）。

    lease_owner：給定時（看板每次 run 產生唯一 owner），配齊後把整組池帳號上租約，
    並行 run 不會再配到同帳號；呼叫端須在 run 結束後 release(owner) 歸還
    （release 也要用同一個 for_system 作用域的 pool，否則 db_name 對不上放不掉）。
    None（CLI / 換號預覽）時行為與既往完全相同：不上租約。
    """
    with _ACTOR_ALLOC_LOCK:
        actors = _actors_for_unlocked(system, s, exclude, required, case_vars)
        if lease_owner:
            ids = sorted({str(a.user_id) for a in actors.values()
                          if getattr(a, "user_id", None)})
            # 池以 db_name 隔離：contract 帳號掛在 contract 庫作用域，租約要寫對作用域
            AccountPool(s.for_system(system)).lease_accounts(ids, owner=lease_owner)
        return actors


def _actors_for_unlocked(system: str, s: Settings,
                         exclude: dict[str, list[str]] | None = None,
                         required: set[str] | None = None,
                         case_vars: dict | None = None) -> dict[str, Actor]:
    """依系統登入對應角色（承攬制 publisher 會做發票 preflight）。

    exclude：{role: [account_id,...]}，配發時跳過這些帳號（「換一個號」用；
    三系統的角色現在都從帳號池配發，contract 用 publisher/receiver 為 key）。
    required：用例引用的具名 actor（required_actors(spec)）。選配 actor（labor3 /
    labor_lacking_*）若在其中即升級為硬需求：配不到立刻拋 PoolShortage，在發佈任何
    工作前就失敗（避免跑到中途 KeyError，白燒一次發佈間隔與當日配對）。
    """
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
        labor_caps = JOB_LABOR_CAPS
        # 配對感知配發（#717 30229）：單帳號 LRU 看不見「夥伴×商家」配對維度，小池會踩回
        # 當日燒過的組合。從今日 qa_runs 還原配對史，硬避已錄取配對、軟避時段衝突與發佈間隔；
        # 規則對應的後端錯誤碼見 _pick_job_pair docstring。
        # employer 要 shop_approved（店鋪 validation_status=3 已過審）才能發佈工作；
        # verified_shop 只代表「公司型已送審」，未過審發佈會 20022（JobForm::validateCanCreateJob）。
        hist = _job_history(s)
        window = _slot_window(case_vars)   # 用例實際時段（明天類用例 preflight 不可用預設包絡）
        emp, la = _pick_job_pair(s, pool, hist, labor_caps, exclude, window)
        ensure_employer_invoice(emp)   # 無發票資訊發佈會 20017（非企業支付都檢查）
        actors = {
            "employer": emp,
            "labor": la[0],
            "labor1": la[0],
            "labor2": la[1],
        }
        # 第三個合格夥伴：排除已用兩位、本商家燒過的配對與時段佔用中的夥伴，再配一個
        # （同樣 preflight 時段衝突——labor3 也會申請，J2 一樣被 30207 擋）。
        # 用例有引用（required）時是硬需求，配不到立即失敗；否則選配、池不足略過。
        try:
            used = [str(a.user_id) for a in la]
            burned3 = _burned_labors(hist, str(emp.user_id), _job_work_date(window))
            busy3 = _busy_labors(hist, window)
            actors["labor3"] = _acquire_clear_labors(
                s, pool, 1, labor_caps,
                (exclude.get("labor") or []) + used + burned3 + busy3, window)[0]
        except PoolShortage as e:
            if "labor3" in required:
                raise PoolShortage(
                    f"用例需要第三位合格夥伴 labor3，但排除本次已用 {used}、本商家當日已錄取"
                    f"配對與時段佔用後，池中已無可配帳號：{e}\n"
                    f"建議：register/provision 補合格 labor 入池，或晚點再跑（佔用釋放）。") from e
        # 負向用例用的「缺能力」夥伴（deficiency actor）：用例有引用時硬要，否則盡力配發。
        # 帳號須「只缺目標能力、其餘前置滿足」才能可靠觸發對應守衛失敗（後端驗證有序）。
        for name, (lack, base) in LABOR_DEFICIENCY_ACTORS.items():
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
    # 承攬制：publisher / receiver 改從帳號池按能力動態配發（不再寫死 audit 帳號）。
    # 池以 db_name 隔離作用域，contract 分庫的帳號掛在 for_system("contract") 的庫名下；
    # API base 兩系統共用，故登入照常。receiver 排除 publisher 已配的號（兩角色互斥），
    # 登入失敗自動換號（_login_from_pool）。發票 preflight 沿用（API 冪等覆寫，不動綁卡）。
    sc = s.for_system("contract")
    cpool = AccountPool(sc)
    publisher = _login_from_pool(sc, cpool, "labor", CONTRACT_PUBLISHER_CAPS, 1,
                                 owner="contract-actors",
                                 exclude=exclude.get("publisher"))[0]
    ensure_publisher_invoice(publisher)
    receiver = _login_from_pool(sc, cpool, "labor", CONTRACT_RECEIVER_CAPS, 1,
                                owner="contract-actors",
                                exclude=(exclude.get("receiver") or []) + [str(publisher.user_id)])[0]
    return _check({"publisher": publisher, "receiver": receiver})


# 負向用例的「缺能力」夥伴（deficiency actor）→ (要缺的能力, 其餘須具備的前置能力)。
# 單一真實來源：_actors_for 配發時用它、case_gen/_derive_children 判「池中是否真有此缺能力帳號」也用它。
# 帳號須「只缺目標能力、其餘前置滿足」才能可靠觸發對應守衛失敗（後端驗證有序）。
LABOR_DEFICIENCY_ACTORS = {
    "labor_lacking_verified": ("verified", ["profile_complete", "active", "clean"]),
    # 缺 profile_complete 但 base 要求 profile_started：精準配「部分填寫」帳號(→30215)，
    # 不會誤配到完全空白帳號(那會回 30211)。空白帳號的負向情境另用 profile_started 缺能力表達。
    "labor_lacking_profile_complete": ("profile_complete", ["profile_started", "active"]),
    # 完全空白帳號（缺 profile_started）：申請工作回 30211（尚未填寫任何個資）。
    "labor_lacking_profile_started": ("profile_started", ["active"]),
}

_SWAPPABLE_LABOR = re.compile(r"labor\d*$")   # labor / labor1..3；labor_lacking_* 是刻意缺能力，不可換


def job_actor_swapper(s: Settings):
    """回傳 RecordingRunner 的自動換號回呼（job 系統；其他系統用 actor_swapper_for）。

    觸發場景（意見反饋）：J2 labor-apply 撞 30229/30213「同一企業/商家每日僅限工作一次」。
    配發層的配對史避撞（_pick_job_pair）對框架外的手動操作、actors 已丟失的舊中斷 run
    是盲的，後端最終裁決才暴露。此時工作已發佈，換 employer 代價高（重發佈＋600s 間隔），
    換夥伴即可：排除當前所有在用帳號＋該商家今日已錄取配對＋時段佔用中的夥伴，
    從池中再配一個時段乾淨的合格夥伴重試本步。配不到（PoolShortage）回 None，照常記失敗。
    """
    def swap(actor_name: str, old: Actor, state) -> Actor | None:
        if not _SWAPPABLE_LABOR.fullmatch(actor_name):
            return None
        pool = AccountPool(s)
        hist = _job_history(s)
        emp = state.actors.get("employer")
        window = _slot_window(getattr(state, "vars", None))   # 依用例 vars 算實際時段
        burned = _burned_labors(hist, str(emp.user_id), _job_work_date(window)) if emp else []
        busy = _busy_labors(hist, window)
        used = [str(getattr(a, "user_id", "")) for a in state.actors.values()]
        try:
            return _acquire_clear_labors(s, pool, 1, JOB_LABOR_CAPS,
                                         used + burned + busy, window)[0]
        except PoolShortage as e:
            print(f"  [swap] 池中無可替補夥伴（排除在用 {used}、已燒配對與佔用後）：{e}")
            return None
    return swap


def actor_swapper_for(system: str, s: Settings):
    """依系統回對應的自動換號回呼；contract 是固定 audit 帳號（無池可換）回 None。"""
    return job_actor_swapper(s) if system == "job" else None


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
    actors = _actors_for(system, s, required=required_actors(spec),
                         case_vars=spec.get("vars"))
    qa = QAStore(s)
    qa.migrate()
    result = RecordingRunner(db, qa_store=qa, system=system,
                             actor_swapper=actor_swapper_for(system, s)).run(spec, actors=actors)

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
