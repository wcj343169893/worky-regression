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

import base64
import json
import random
import time
from dataclasses import dataclass
from typing import Any

import sqlalchemy
from sqlalchemy import text

from .client import WorkyClient, md5
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
    # 姓名只在拿得到解密值的時點寫（API 註冊讀 /profile）；gender 工作庫明文可同步探回。
    display_name: str | None = None
    gender: int | None = None


# 已知測試帳號種子（id 與登入資料）。能力(caps)由 sync 探測，不寫死在這裡。
# labor：audit 打工夥伴；employer：audit 商家。
SEED_LABOR_IDS = [236, 276, 365, 15, 214, 373]
SEED_EMPLOYERS = [
    {"account_id": 129, "phone": "0923113000", "username": "886923113000", "shop_id": 70},
]
# 供給階段可校正硬狀態的測試帳號白名單（只動這些測試帳號，不波及真實用戶）。
PROVISION_LABOR_IDS = [236, 276, 365, 15, 214]
# 各角色的「種子容量」＝池可能擁有的帳號數上限（池是固定 audit 種子、不註冊新帳號）。
# 補池低標不可超過容量，否則容量小的角色（如 employer 只 1 個）永遠達不到低標 → 無限空轉。
SEED_CAPACITY = {"labor": len(SEED_LABOR_IDS), "employer": len(SEED_EMPLOYERS)}
# API 建店鋪/送審需上傳圖片：用 1x1 透明 PNG 充當（後端只要有效圖檔，不檢內容）。
_DUMMY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC")

# 身分證英文字母加權對照（worky common/validators/IdNumberValidator.php）。
_ID_LETTER = {'A': 10, 'B': 11, 'C': 12, 'D': 13, 'E': 14, 'F': 15, 'G': 16, 'H': 17,
              'I': 34, 'J': 18, 'K': 19, 'L': 20, 'M': 21, 'N': 22, 'O': 35, 'P': 23,
              'Q': 24, 'R': 25, 'S': 26, 'T': 27, 'U': 28, 'V': 29, 'W': 32, 'X': 30,
              'Y': 31, 'Z': 33}


class AccountPool:
    """帳號池存取層。執行期(acquire/release)只碰 QA 庫；供給/同步才連工作庫。"""

    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def _qa_engine(self):
        return qa_models.get_engine(self.s)

    @property
    def db(self) -> str:
        """目前被測庫名：池內帳號按此隔離（切庫＝換一套帳號）。"""
        return self.s.db_name

    def _worky_engine(self):
        """連工作庫（僅供給/同步用；執行期不呼叫）。"""
        s = self.s
        url = f"mysql+pymysql://{s.db_user}:{s.db_pass}@{s.db_host}:{s.db_port}/{s.db_name}?charset=utf8mb4"
        return sqlalchemy.create_engine(url, future=True)

    # ── 執行期：配發 / 歸還（只讀寫 QA 庫）──────────────────────────────────────
    def acquire(self, role: str, caps_required: list[str], n: int, *,
                owner: str, lease_secs: int = 900, lease: bool = True,
                exclude: list[str] | None = None) -> list[PooledAccount]:
        """配發 n 個 role 帳號，需具備 caps_required 全部能力；不足則 PoolShortage。

        lease=True：加軟租約（available 或租約過期者可被借，借走標 leased + 到期）。
        lease=False：純選取不上鎖（同步循序執行時用，避免反覆 run 互卡）。
        exclude：要跳過的 account_id 清單（「換一個號」用——排除目前出問題的帳號）。
        """
        now = int(time.time())
        want = set(caps_required)
        skip = {str(x) for x in (exclude or [])}
        with self._qa_engine.begin() as conn:
            # 排序：① 同 owner 已租過的優先（同一 run 內取回原帳號）；
            #      ② 其次「最久未用優先」(last_used_at ASC) → 在多帳號時自動輪換，
            #         分散每商家每日發佈上限（避免 20020 刊登中工作超過上限）；
            #      ③ 最後 account_id 穩定排序。
            rows = conn.execute(text("""
                SELECT id, account_id, role, user_type, phone, username, shop_id, caps, note
                FROM qa_accounts
                WHERE db_name=:db AND role=:role AND state<>'disabled'
                  AND (state='available' OR lease_expires_at < :now)
                ORDER BY (lease_owner=:owner) DESC, last_used_at ASC, account_id ASC
            """), {"db": self.db, "role": role, "now": now, "owner": owner}).all()
            def _caps(r) -> list[str]:
                c = r.caps
                return json.loads(c) if isinstance(c, str) else (c or [])

            chosen = []
            for r in rows:
                if str(r.account_id) in skip:
                    continue
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
            # 標記「最近使用時間」→ 下次配發走最久未用優先輪換（與 lease 無關，純輪換用）
            for r in chosen:
                conn.execute(text(
                    "UPDATE qa_accounts SET last_used_at=:now WHERE id=:id"), {"now": now, "id": r.id})
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
                WHERE db_name=:db AND role=:role AND state<>'disabled'
                  AND (state='available' OR lease_expires_at < :now)
                ORDER BY last_used_at ASC, account_id ASC
            """), {"db": self.db, "role": role, "now": now}).all()

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
            for r in chosen:
                conn.execute(text(
                    "UPDATE qa_accounts SET last_used_at=:now WHERE id=:id"), {"now": now, "id": r.id})
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

    # ── 本地 token 快取（配發時「有效就用、到期才刷」）─────────────────────────
    def load_token(self, account_id: int, role: str) -> dict[str, Any] | None:
        """讀帳號池內保存的 token（不檢查是否過期，由呼叫端的 client 自行判斷）。

        token 與簽發時的 API base 綁定（/v1 與 /qa-v1 模組 requestSource 不同，token 互不
        通用，混用會 10003）：存的 base 與當前 .env 不符（含舊資料的 NULL）→ 視為無快取。
        """
        with self._qa_engine.connect() as conn:
            r = conn.execute(text(
                "SELECT access_token, refresh_token, access_token_expired_at, "
                "refresh_token_expired_at, token_api_base FROM qa_accounts "
                "WHERE db_name=:db AND account_id=:a AND role=:r"),
                {"db": self.db, "a": int(account_id), "r": role}).first()
        if not r:
            return None
        m = dict(r._mapping)
        if m.pop("token_api_base", None) != self.s.api_base:
            return None
        return m

    def save_token(self, account_id: int, role: str, *, access_token: str,
                   refresh_token: str, access_expired_at: int, refresh_expired_at: int) -> None:
        """把登入/刷新後的最新 token 寫回帳號池，供下次配發重用。"""
        now = int(time.time())
        with self._qa_engine.begin() as conn:
            conn.execute(text("""
                UPDATE qa_accounts SET
                  access_token=:at, refresh_token=:rt,
                  access_token_expired_at=:aexp, refresh_token_expired_at=:rexp,
                  token_updated_at=:now, token_api_base=:base
                WHERE db_name=:db AND account_id=:a AND role=:r
            """), {
                "at": access_token, "rt": refresh_token,
                "aexp": int(access_expired_at or 0), "rexp": int(refresh_expired_at or 0),
                "now": now, "base": self.s.api_base,
                "db": self.db, "a": int(account_id), "r": role,
            })

    def release(self, owner: str) -> int:
        """歸還某 owner 借走的所有帳號。"""
        with self._qa_engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_accounts SET state='available', lease_owner=NULL, lease_expires_at=0 "
                "WHERE db_name=:db AND lease_owner=:o"), {"db": self.db, "o": owner})
            return res.rowcount

    # ── 動態補池（worker 用：偵測可用數不足 → 回收過期租約 + provision 補回）──────
    def available_count(self) -> dict[str, int]:
        """各角色『目前可配發』數：state='available' 或租約已過期者都算可配發。

        與 acquire 的可借判定一致（available 或 lease 過期），故能反映 worker 該不該補。
        """
        now = int(time.time())
        with self._qa_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT role, "
                "SUM(CASE WHEN state<>'disabled' AND (state='available' OR lease_expires_at < :now) "
                "         THEN 1 ELSE 0 END) AS avail "
                "FROM qa_accounts WHERE db_name=:db GROUP BY role"),
                {"db": self.db, "now": now}).all()
        return {r.role: int(r.avail or 0) for r in rows}

    def reclaim_expired_leases(self) -> int:
        """把租約已過期但 state 仍卡 'leased' 的帳號改回 available。回收筆數。

        acquire 雖把過期租約視為可借，但 state 欄沒回 available，看板/統計會低估可用數；
        這裡顯式回收，讓「可用數」如實反映、也讓 worker 的補池判定不被殘留租約誤導。
        """
        now = int(time.time())
        with self._qa_engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_accounts SET state='available', lease_owner=NULL, lease_expires_at=0 "
                "WHERE db_name=:db AND state='leased' AND lease_expires_at < :now"),
                {"db": self.db, "now": now})
            return res.rowcount

    def top_up(self, *, min_available: int = 3, heal: bool = True) -> dict[str, Any]:
        """動態補池：先回收過期租約；若仍有角色可配發數 < min_available 則跑 provision()。

        provision() 會解停權 / 上架 audit role + sync_caps，把卡住的種子帳號修回可配發。
        因池是固定 audit 種子帳號（不註冊新帳號），「補一批」= 把流失的種子救回 available。
        回傳摘要供 worker 記錄；無角色不足時不連工作庫（provisioned=None）。
        """
        reclaimed = self.reclaim_expired_leases()
        before = self.available_count()
        # 每角色有效低標 = min(min_available, 種子容量)：容量小的角色（employer 只 1 個）
        # 達到容量即視為滿，不再 provision，避免「永遠 < 低標」的無限補池空轉。
        targets = {r: min(min_available, SEED_CAPACITY.get(r, min_available)) for r in ("labor", "employer")}
        low = {r: before.get(r, 0) for r in ("labor", "employer") if before.get(r, 0) < targets[r]}
        result: dict[str, Any] = {
            "reclaimed": reclaimed, "before": before,
            "min_available": min_available, "targets": targets, "low": low, "provisioned": None,
        }
        if low:
            result["provisioned"] = self.provision(heal=heal)
            result["after"] = self.available_count()
        return result

    def list_all(self) -> list[dict[str, Any]]:
        with self._qa_engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(text(
                "SELECT account_id, role, caps, state, note FROM qa_accounts "
                "WHERE db_name=:db ORDER BY role, account_id"), {"db": self.db}).all()]

    @staticmethod
    def _norm_caps(c) -> list[str]:
        return json.loads(c) if isinstance(c, str) else (c or [])

    def list_pool(self) -> list[dict[str, Any]]:
        """看板帳號池管理頁用：完整欄位（含 user_type/phone/shop/租約/最近使用）。"""
        now = int(time.time())
        with self._qa_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT account_id, role, user_type, phone, username, display_name, gender, "
                "shop_id, caps, state, note, lease_owner, lease_expires_at, last_used_at "
                "FROM qa_accounts WHERE db_name=:db ORDER BY role, account_id"),
                {"db": self.db}).all()
        out = []
        for r in rows:
            m = dict(r._mapping)
            m["caps"] = self._norm_caps(m.get("caps"))
            # 租約是否仍有效（leased 且未過期）——供前端標示「使用中」
            m["leased_active"] = bool(m.get("lease_owner")) and int(m.get("lease_expires_at") or 0) > now
            out.append(m)
        return out

    def get(self, account_id: int, role: str) -> dict[str, Any] | None:
        with self._qa_engine.connect() as conn:
            r = conn.execute(text(
                "SELECT account_id, role, user_type, phone, username, display_name, gender, "
                "shop_id, caps, state, note "
                "FROM qa_accounts WHERE db_name=:db AND account_id=:a AND role=:r"),
                {"db": self.db, "a": int(account_id), "r": role}).first()
        if not r:
            return None
        m = dict(r._mapping)
        m["caps"] = self._norm_caps(m.get("caps"))
        return m

    def set_state(self, account_id: int, role: str, state: str) -> int:
        """啟用/停用帳號（available / disabled）；disabled 者 acquire 不會配發。回更新列數。"""
        if state not in ("available", "disabled"):
            raise ValueError("state 只能 available / disabled")
        with self._qa_engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_accounts SET state=:s WHERE db_name=:db AND account_id=:a AND role=:r"),
                {"s": state, "db": self.db, "a": int(account_id), "r": role})
            return res.rowcount

    def clear(self, role: str | None = None) -> int:
        """清空當前庫的池**追蹤列**（qa_accounts），不動後端真實帳號。回刪除列數。

        role 省略＝labor+employer 全清；種子可事後 provision()/sync_caps() 重新探測補回。
        """
        sql = "DELETE FROM qa_accounts WHERE db_name=:db"
        params: dict[str, Any] = {"db": self.db}
        if role is not None:
            if role not in ("labor", "employer"):
                raise ValueError("role 只能 labor / employer")
            sql += " AND role=:r"
            params["r"] = role
        with self._qa_engine.begin() as conn:
            return conn.execute(text(sql), params).rowcount

    # ── 純 API 自助建帳號入池（不連工作庫；09 手機號自動註冊 + 補資料）──────────────
    # 動機：框架無讀工作庫權限時，靠 API 自己造帳號。dev/測試環境註冊回應會帶驗證碼(code)，
    # 故全程 API 即可：產 09 手機號 → register → register/confirm(md5 碼) → 補資料 → 讀 profile
    # 取真實 id 與狀態 → upsert qa_accounts。純 API 達不到 verified/audit_role(需審核/工作庫)，
    # 故只標基本 caps；那類用例仍用既有 audit 種子。
    @staticmethod
    def _gen_phone() -> str:
        """產 09 開頭的 10 位測試手機號（09 + 8 位數字）。"""
        return "09" + "".join(random.choice("0123456789") for _ in range(8))

    def _register_and_login(self, c: WorkyClient, base: str, attempts: int = 6) -> str:
        """產號→register→confirm→把 token 灌進 client，回成功的手機號。撞號/失敗自動換號重試。"""
        last = "未知錯誤"
        for _ in range(attempts):
            phone = self._gen_phone()
            rd = c.post(f"{base}/register", body={"phone": phone}).json()
            code = (rd.get("data") or {}).get("code")
            if not rd.get("success") or not code:
                last = rd.get("message") or "register 未回 code（撞號或非測試環境）"
                continue
            cf = c.post(f"{base}/register/confirm",
                        body={"phone": phone, "password": md5(str(code))}).json()
            d = cf.get("data") or {}
            tok = d.get("accessToken") or d.get("access_token")
            if not cf.get("success") or not tok:
                last = cf.get("message") or "confirm 失敗 / 無 accessToken"
                continue
            c.set_access_token(token=tok, expired_at=d.get("accessTokenExpiredAt", 0),
                               refresh_token=d.get("refreshToken", ""),
                               refresh_expired_at=d.get("refreshTokenExpiredAt", 0))
            return phone
        raise RuntimeError(f"註冊失敗（試 {attempts} 次）：{last}")

    def _complete_labor_demographics(self, c: WorkyClient) -> None:
        """補 labor 強制輪廓資料（性別/出生年/居住地）；縣市/區域取自 /labor/options（代碼環境相關）。"""
        opt = (c.get("/labor/options").json().get("data") or {})
        cds = opt.get("city_districts") or []
        if not cds or not (cds[0].get("districts")):
            raise RuntimeError("取不到縣市/區域選項，無法補輪廓資料")
        city = cds[0]
        dist = city["districts"][0]
        r = c.post("/labor/demographics/create", body={
            "gender": random.choice(["male", "female"]),
            "birth_year": random.randint(1985, 2002),
            "city": city["id"], "district": dist["id"],
        }).json()
        if not r.get("success"):
            raise RuntimeError(f"demographics 失敗：{r.get('message')}")

    @staticmethod
    def _gen_id_number(second_choices: str) -> str:
        """產通過 IdNumberValidator 的身分證號（10 碼，含檢查碼）。

        labor 第二碼限 '12'（`_gen_id_number("12")`）；employer 限 '1289ABCD'。
        驗證器把第二碼 A–D 視為 0–3 後加權，故 sec_val 需相應換算。
        """
        while True:
            first = random.choice(list(_ID_LETTER))
            second = random.choice(second_choices)
            sec_val = (ord(second) - ord('A')) if 'A' <= second <= 'D' else int(second)
            mids = [random.randint(0, 9) for _ in range(7)]   # idArray[2..8]
            num = _ID_LETTER[first]
            point = num // 10 + (num % 10) * 9                 # 字母加權
            point += sec_val * 8                               # idArray[1] 權重 (9-1)
            for i, d in enumerate(mids):                       # idArray[2..8] 權重 7..1
                point += d * (7 - i)
            check = (10 - point % 10) % 10                     # idArray[9] 檢查碼
            idn = first + second + "".join(map(str, mids)) + str(check)
            if len(idn) == 10:
                return idn

    def _complete_labor_full_profile(self, c: WorkyClient, phone: str) -> None:
        """填齊送審必填三分群（個人/聯繫/薪資帳戶）+ `is_submitted_for_review=true`。

        成功後工作庫 `is_profile_complete=1`、`valid_status=2`(待認證)；再經後台核准才得 verified。
        身分證正反面 / 存摺封面用佔位圖上傳（uploader type 須對）。
        """
        opt = (c.get("/labor/options").json().get("data") or {})
        cds = opt.get("city_districts") or []
        if not cds or not cds[0].get("districts"):
            raise RuntimeError("取不到縣市/區域選項，無法送審")
        city = cds[0]; dist = city["districts"][0]
        banks = opt.get("banks") or []
        if not banks:
            raise RuntimeError("取不到銀行選項，無法填薪資帳戶")
        bank_code = banks[0]["code"]
        suffix = phone[-4:]
        front = self._upload_image(c, "labor_id_card_image")
        back = self._upload_image(c, "labor_id_card_image")
        book = self._upload_image(c, "labor_passbook_cover_image")
        r = c.post("/labor/update", body={
            "display_name": f"QA{suffix}", "gender": random.choice(["male", "female"]),
            "birthday": f"{random.randint(1985, 2002)}-05-05",
            "id_number": self._gen_id_number("12"),
            "id_card_front_image": front, "id_card_back_image": back,
            "email": f"qa{phone[-6:]}@worky.local",
            "city": city["id"], "district": dist["id"], "address": "測試路1號",
            "emergency_contact_person": "測試聯絡人", "emergency_contact_relation": "親屬",
            "emergency_contact_phone": "0933123123",
            "bank_account_name": f"QA{suffix}", "bank_code": bank_code,
            "bank_branch_code": f"{bank_code}0011", "bank_account": "012345678901",
            "passbook_cover_image": book, "is_submitted_for_review": True,
        }).json()
        if not r.get("success"):
            raise RuntimeError(f"完整資料送審失敗：{r.get('message')}")

    @staticmethod
    def _profile(c: WorkyClient, base: str) -> dict:
        """讀 /labor|/employer/profile，回 data（含真實 id / status / valid_status）。"""
        r = c.get(f"{base}/profile").json()
        d = r.get("data") if isinstance(r.get("data"), dict) else None
        if not r.get("success") or not d or not d.get("id"):
            raise RuntimeError(f"profile 查詢失敗：{r.get('message')}")
        return d

    @staticmethod
    def _caps_from_profile(role: str, prof: dict) -> list[str]:
        """由 profile 推 caps，一律**依實際欄位查證**（不假設）。

        與工作庫探測 _probe_labor_caps 同義，避免 caps 標了卻名不副實：
          active=status==10 / profile_complete=is_profile_complete==1 /
          verified=valid_status==1 / clean=無違規點數(penalty_points 為 0)。
        注意 demographics/create 不等於 is_profile_complete=1（後端另有完整度判定），故必須讀回實況。
        """
        caps: list[str] = []
        if prof.get("status") == 10:
            caps.append("active")
        if role == "labor":
            if prof.get("is_profile_complete") == 1:
                caps.append("profile_complete")
            if prof.get("valid_status") == 1:
                caps.append("verified")
            if not prof.get("penalty_points"):   # 0 / None → 無違規點數
                caps.append("clean")
        return caps

    def register_via_api(self, role: str, n: int = 1,
                         caps: list[str] | None = None) -> list[dict[str, Any]]:
        """純 API 自助建 n 個 role 帳號入池。逐筆隔離，單筆失敗不中斷其餘。

        caps＝**目標能力**，決定 API 端要做到哪一步（後台核准類 verified/shop_approved 仍由
        service 的 auto_review 串接，這裡只負責 API 能造出的前置資料）：
          · labor 含 'profile_complete' → 走完整資料送審（否則只補 demographics）
          · employer 含 'verified_shop' → 店鋪以 validation_type=2 送審（否則統編 type=1）
        回 [{role, ok, phone?, account_id?, caps?, error?}, …]。
        """
        if role not in ("labor", "employer"):
            raise ValueError("role 只能 labor / employer")
        want = set(caps or [])
        user_type = 2 if role == "labor" else 1
        base = "/labor" if role == "labor" else "/employer"
        out: list[dict[str, Any]] = []
        for _ in range(max(1, n)):
            res: dict[str, Any] = {"role": role, "ok": False}
            try:
                c = WorkyClient(self.s, user_type=user_type)
                phone = self._register_and_login(c, base)
                res["phone"] = phone
                shop_id = None
                note = "api"
                if role == "labor":
                    if "profile_complete" in want:
                        self._complete_labor_full_profile(c, phone)   # → is_profile_complete=1, 待認證
                    else:
                        self._complete_labor_demographics(c)
                else:
                    # employer 建店鋪並送審（純 API）；verified_shop 目標 → validation_type=2。失敗不影響入池
                    shop_id, shop_note = self._provision_employer_shop(
                        c, phone, vtype2=("verified_shop" in want))
                    res["shop_id"] = shop_id
                    res["shop"] = shop_note
                    note = f"api;{shop_note}"
                prof = self._profile(c, base)
                acc_id = int(prof["id"])
                caps = self._caps_from_profile(role, prof)
                self._upsert_api_account(account_id=acc_id, role=role, user_type=user_type,
                                         phone=phone, username=prof.get("username"),
                                         shop_id=shop_id, caps=caps, note=note,
                                         display_name=prof.get("display_name"),
                                         gender=prof.get("gender"))
                res.update(ok=True, account_id=acc_id, caps=caps)
            except Exception as e:  # noqa: BLE001 — 單筆失敗收集後續顯示，不打斷整批
                res["error"] = f"{type(e).__name__}: {e}"
            out.append(res)
        return out

    @staticmethod
    def _gen_tax_id() -> str:
        """產一個通過檢查碼的台灣統一編號（8 位；2023 新制除數 5，含第 7 位為 7 的特例）。"""
        w = [1, 2, 1, 2, 1, 2, 4, 1]
        while True:
            d = [random.randint(0, 9) for _ in range(8)]
            sm = sum((d[i] * w[i]) // 10 + (d[i] * w[i]) % 10 for i in range(8))
            if sm % 5 == 0 or (d[6] == 7 and (sm + 1) % 5 == 0):
                return "".join(map(str, d))

    def _provision_employer_shop(self, c: WorkyClient, phone: str,
                                 vtype2: bool = False) -> tuple[int | None, str]:
        """employer 建店鋪 + 送審（純 API）。回 (shop_id, note)；任一步失敗回 (None, 失敗說明)。

        步驟：上傳 logo → /employer/shop/create → 上傳審核圖 → /employer/shop/validation/request
        (is_draft=false 即送審)。仍需後台核准才得 shop_approved，本步只到「已送審」。
        vtype2=True → validation_type=2（身分證號送審），對應 caps 的 **verified_shop**（探測以 type==2 認定）；
        否則 validation_type=1（統一編號）。
        """
        try:
            opt = c.get("/employer/options").json().get("data") or {}
            cds = opt.get("city_districts") or []
            if not cds or not cds[0].get("districts"):
                return None, "shop_failed:無縣市/區域選項"
            city = cds[0]
            dist = city["districts"][0]
            jt = (opt.get("job_types") or [{}])[0]
            suffix = phone[-4:]
            logo = self._upload_image(c, "shop_company_logo_image")
            sc = c.post("/employer/shop/create", body={
                "name": f"QA測試店鋪{suffix}", "city": city["id"], "district": dist["id"],
                "address": "測試路1號", "job_type_level1": jt.get("id"),
                "email": f"qa{phone[-6:]}@worky.local", "mobile_phone": phone,
                "company_logo": logo,
            }).json()
            if not sc.get("success"):
                return None, f"shop_failed:{sc.get('message')}"
            shop_id = (sc.get("data") or {}).get("shop_id") or sc.get("shopId")
            vpic = self._upload_image(c, "shop_verify_image")
            ident = ({"validation_type": 2, "id_number": self._gen_id_number("1289ABCD"),
                      "tax_id_number": ""} if vtype2 else
                     {"validation_type": 1, "tax_id_number": self._gen_tax_id(), "id_number": ""})
            sub = c.post("/employer/shop/validation/request", body={
                "shop_id": shop_id, "verify_name": f"QA測試店鋪{suffix}",
                "verify_city": city["id"], "verify_district": dist["id"], "verify_address": "測試路1號",
                "is_draft": False, "verify_pic_1": vpic, **ident,
            }).json()
            if not sub.get("success"):
                return shop_id, f"shop_submit_failed:{sub.get('message')}"
            return shop_id, ("shop_submitted_v2" if vtype2 else "shop_submitted")
        except Exception as e:  # noqa: BLE001
            return None, f"shop_error:{type(e).__name__}"

    @staticmethod
    def _upload_image(c: WorkyClient, file_type: str) -> str:
        """上傳一張佔位圖，回網址；失敗則拋。"""
        r = c.upload_file(file_type, _DUMMY_PNG).json()
        uf = (r.get("data") or {}).get("uploadedFiles")
        if not r.get("success") or not isinstance(uf, list) or not uf:
            raise RuntimeError(f"上傳 {file_type} 失敗：{r.get('message')}")
        return uf[0]

    def _upsert_api_account(self, *, account_id: int, role: str, user_type: int, phone: str,
                            username: str | None, shop_id: int | None, caps: list[str],
                            note: str = "api", display_name: str | None = None,
                            gender: int | None = None) -> None:
        """把 API 建出的帳號 upsert 進池；note 以 'api' 開頭與 audit 種子區分。

        display_name/gender 來自 /profile（解密後明文）；COALESCE 保留既有值，沒拿到不抹掉。
        """
        now = int(time.time())
        with self._qa_engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO qa_accounts
                  (db_name, account_id, role, user_type, phone, username, display_name, gender,
                   shop_id, caps, state, note, synced_at)
                VALUES (:db, :account_id, :role, :user_type, :phone, :username, :display_name,
                        :gender, :shop_id, :caps, 'available', :note, :now)
                ON DUPLICATE KEY UPDATE
                  user_type=VALUES(user_type), phone=VALUES(phone), username=VALUES(username),
                  display_name=COALESCE(VALUES(display_name), display_name),
                  gender=COALESCE(VALUES(gender), gender),
                  shop_id=VALUES(shop_id), caps=VALUES(caps), note=VALUES(note), synced_at=VALUES(synced_at)
            """), {"db": self.db, "account_id": account_id, "role": role, "user_type": user_type,
                   "phone": phone, "username": username, "display_name": display_name,
                   "gender": gender, "shop_id": shop_id,
                   "caps": json.dumps(caps), "note": note, "now": now})

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

    # ── 能力探測（單一真實來源：caps 一律由工作庫實況推出，供 sync_caps 與 sync_account_caps 共用）──
    @staticmethod
    def _probe_labor_caps(wc, uid: int) -> tuple[list[str], str | None] | None:
        """探測 labor 工作庫能力 → (caps, note)；帳號不存在回 None。"""
        now = int(time.time())
        r = wc.execute(text(
            "SELECT valid_status, is_profile_complete FROM s_labors WHERE id=:i"), {"i": uid}).first()
        if not r:
            return None
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
        if r.valid_status == 1: caps.append("verified")     # 後台審核通過 → valid_status=1
        if r.is_profile_complete == 1: caps.append("profile_complete")
        if published: caps.append("audit_role")
        if not suspended: caps.append("active")
        # clean = 無違規點數歷史。有違規的帳號(殘留停權/扣點)在 apply 會以泛用 10001 失敗，
        # DB 欄位看不出來，故獨立成一個能力讓申請者用例避開。
        if penalty == 0: caps.append("clean")
        notes = []
        if suspended: notes.append("suspended")
        if penalty: notes.append(f"penalty_logs={penalty}")
        return caps, (";".join(notes) or None)

    @staticmethod
    def _probe_employer_caps(wc, uid: int, shop_id: int | None) -> tuple[list[str], str | None] | None:
        """探測 employer 工作庫能力 → (caps, note)；帳號不存在回 None。

        verified_shop＝該店鋪為公司型(validation_type=2)；shop_approved＝店鋪已通過審核(validation_status=3)。
        """
        e = wc.execute(text("SELECT id FROM s_employers WHERE id=:i"), {"i": uid}).first()
        if not e:
            return None
        caps = ["active"]
        if shop_id:
            shop = wc.execute(text(
                "SELECT validation_type, validation_status FROM s_shops WHERE id=:s"),
                {"s": shop_id}).first()
            if shop:
                if shop.validation_type == 2: caps.append("verified_shop")
                if shop.validation_status == 3: caps.append("shop_approved")  # 後台審核通過
        return caps, None

    def sync_account_caps(self, account_id: int, role: str) -> dict | None:
        """重探單一帳號的工作庫狀態重算 caps，寫回 qa_accounts。

        供後台審核成功後即時更新（caps 真相＝工作庫實況，與 sync_caps 同源）。
        僅當該帳號在「當前庫」的池中才更新；不在池中（多數被審核對象是隨機測試資料）回 None。
        """
        existing = self.get(account_id, role)
        if existing is None:
            return None
        weng = self._worky_engine()
        try:
            with weng.connect() as wc:
                probed = (self._probe_labor_caps(wc, int(account_id)) if role == "labor"
                          else self._probe_employer_caps(wc, int(account_id), existing.get("shop_id")))
        finally:
            weng.dispose()
        if probed is None:
            caps, note = (existing.get("caps") or []), "missing_in_worky"
        else:
            caps, note = probed
        # 保留 'api' 標記（api 自助註冊帳號）：否則重探會抹掉 → 下次 reload(note LIKE 'api%') 找不到
        if str(existing.get("note") or "").strip().lower().startswith("api"):
            note = "api" if not note else f"api;{note}"
        now = int(time.time())
        with self._qa_engine.begin() as conn:
            conn.execute(text(
                "UPDATE qa_accounts SET caps=:caps, note=:note, synced_at=:now "
                "WHERE db_name=:db AND account_id=:a AND role=:r"),
                {"caps": json.dumps(caps), "note": note, "now": now,
                 "db": self.db, "a": int(account_id), "r": role})
        return {"account_id": int(account_id), "role": role, "caps": caps, "note": note}

    def resync_shop_owner(self, shop_id: int) -> dict | None:
        """店鋪審核後用：找當前庫池中記錄該 shop_id 的商家，重探其 caps（更新 shop_approved 等）。

        多數被審核店鋪不在池中 → 回 None（no-op）。
        """
        with self._qa_engine.connect() as conn:
            r = conn.execute(text(
                "SELECT account_id FROM qa_accounts "
                "WHERE db_name=:db AND role='employer' AND shop_id=:s LIMIT 1"),
                {"db": self.db, "s": int(shop_id)}).first()
        if not r:
            return None
        return self.sync_account_caps(r.account_id, "employer")

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
                    # gender 工作庫是明文可直接探回；display_name 加密讀不到，不在此同步
                    info = wc.execute(text(
                        "SELECT phone, username, gender FROM s_labors WHERE id=:i"), {"i": uid}).first()
                    if not info:
                        continue
                    caps, note = self._probe_labor_caps(wc, uid)
                    seeds.append(PooledAccount(
                        account_id=uid, role="labor", user_type=2, phone=info.phone or "",
                        username=info.username, shop_id=None, caps=caps, note=note,
                        gender=info.gender))
                # ── employer（商家）──
                for emp in SEED_EMPLOYERS:
                    uid = emp["account_id"]
                    probed = self._probe_employer_caps(wc, uid, emp["shop_id"])
                    caps, note = probed if probed else (["active"], "missing_in_worky")
                    seeds.append(PooledAccount(
                        account_id=uid, role="employer", user_type=1, phone=emp["phone"],
                        username=emp["username"], shop_id=emp["shop_id"], caps=caps, note=note))

                # ── 既有 api 自助註冊帳號：同樣依工作庫實況重探 caps（種子以外，note 以 'api' 開頭）──
                # 否則 register 當下標的 caps 永遠不會被「重載」修正（如 #1569 誤標 profile_complete）。
                with self._qa_engine.connect() as qc:
                    api_rows = qc.execute(text(
                        "SELECT account_id, role, user_type, phone, username, shop_id FROM qa_accounts "
                        "WHERE db_name=:db AND note LIKE 'api%'"), {"db": self.db}).all()
                for r in api_rows:
                    gender = None
                    if r.role == "labor":
                        probed = self._probe_labor_caps(wc, int(r.account_id))
                        g = wc.execute(text("SELECT gender FROM s_labors WHERE id=:i"),
                                       {"i": int(r.account_id)}).first()
                        gender = g.gender if g else None
                    else:
                        probed = self._probe_employer_caps(wc, int(r.account_id), r.shop_id)
                    if probed is None:
                        caps, pnote = ["active"], "missing_in_worky"
                    else:
                        caps, pnote = probed
                    # 保留 'api' 前綴（下次重載仍認得；並附帶探測註記）
                    note = "api" if not pnote else f"api;{pnote}"
                    seeds.append(PooledAccount(
                        account_id=int(r.account_id), role=r.role, user_type=r.user_type,
                        phone=r.phone or "", username=r.username, shop_id=r.shop_id,
                        caps=caps, note=note, gender=gender))

            with self._qa_engine.begin() as qc:
                for a in seeds:
                    qc.execute(text("""
                        INSERT INTO qa_accounts
                          (db_name, account_id, role, user_type, phone, username, gender, shop_id, caps, state, note, synced_at)
                        VALUES (:db, :account_id, :role, :user_type, :phone, :username, :gender, :shop_id, :caps, 'available', :note, :now)
                        ON DUPLICATE KEY UPDATE
                          user_type=VALUES(user_type), phone=VALUES(phone), username=VALUES(username),
                          gender=COALESCE(VALUES(gender), gender),
                          shop_id=VALUES(shop_id), caps=VALUES(caps), note=VALUES(note), synced_at=VALUES(synced_at)
                    """), {
                        "db": self.db,
                        "account_id": a.account_id, "role": a.role, "user_type": a.user_type,
                        "phone": a.phone, "username": a.username, "gender": a.gender,
                        "shop_id": a.shop_id,
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
        python -m worky_regression.qa_accounts topup        # 回收過期租約 + 可用不足才 provision
        python -m worky_regression.qa_accounts register --role labor --n 3   # 純 API 自助建帳號入池
        python -m worky_regression.qa_accounts list         # 檢視池現況
    """
    import argparse

    ap = argparse.ArgumentParser(prog="worky-qa-accounts")
    ap.add_argument("cmd", choices=["provision", "sync", "topup", "register", "list"], default="list", nargs="?")
    ap.add_argument("--no-heal", action="store_true", help="provision 時不校正硬狀態")
    ap.add_argument("--min-available", type=int, default=3, help="topup 時每角色可用數低標（預設 3）")
    ap.add_argument("--role", choices=["labor", "employer"], default="labor", help="register 的角色")
    ap.add_argument("--n", type=int, default=1, help="register 要建立的帳號數")
    ap.add_argument("--review", action="store_true",
                    help="register 後順帶用後台管理員審核通過（帳密未設則跳過）")
    args = ap.parse_args(argv)

    pool = AccountPool(Settings.from_env())
    if args.cmd == "provision":
        print(pool.provision(heal=not args.no_heal))
    elif args.cmd == "sync":
        print({"synced": pool.sync_caps()})
    elif args.cmd == "topup":
        print(pool.top_up(min_available=args.min_available, heal=not args.no_heal))
    elif args.cmd == "register":
        results = pool.register_via_api(args.role, args.n)
        if args.review:
            # 審核串接點集中在看板 service（後台管理員 client + caps 重探）；CLI 借用同一邏輯
            from .dashboard.service import DashboardService
            DashboardService(pool.s).auto_review_registered(results)
        ok = sum(1 for r in results if r.get("ok"))
        print(f"[register] {args.role}：{ok}/{len(results)} 成功")
        for r in results:
            if r.get("ok"):
                rev = f" review={r['review']}" if r.get("review") else ""
                print(f"  ✓ #{r['account_id']} {r['phone']} caps={r['caps']}{rev}")
            else:
                print(f"  ✗ {r.get('phone', '?')} 失敗：{r.get('error')}")
    for a in pool.list_all():
        print(f"  {a['role']:8} {a['account_id']:>5}  {a['state']:9} {a['caps']}  {a['note'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
