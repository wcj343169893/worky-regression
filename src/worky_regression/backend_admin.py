"""後台管理員 client：以帳密登入 Yii2 後台（backend.*.worky.com.tw），代為審核。

與 `client.py`（打 /v1 API、自簽名）不同，後台是 **Yii2 session + CSRF 表單登入**：
  1. GET /site/login 取 CSRF token（meta）+ session cookie。
  2. POST /site/login（LoginForm[username]/[password] + csrf）→ 302 回首頁視為成功。
  3. 審核打工夥伴：POST /labor/list/{validate,rejection}（回 JSON code）。
  4. 審核店鋪：先 GET /employer/shop/validation 鎖定為 REVIEWING，再 POST {approve,reject}（回 redirect）。

僅以管理員身分呼叫被測倉既有端點，不修改被測對象（CLAUDE.md）。
"""
from __future__ import annotations

import re

import requests

# Yii2 後台 CSRF 參數名（backend/config/main.php: 'csrfParam' => '_worky-csrf-backend'）
CSRF_PARAM = "_worky-csrf-backend"
# Labor::VALIDATE_ENABLED（common/models/Labor/Labor.php）
LABOR_VALIDATE_ENABLED = 1

_META_TOKEN = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', re.I)
_META_PARAM = re.compile(r'<meta\s+name="csrf-param"\s+content="([^"]+)"', re.I)
_HIDDEN_TOKEN = re.compile(
    r'name="' + re.escape(CSRF_PARAM) + r'"[^>]*value="([^"]+)"', re.I)
_LOGIN_FORM = re.compile(r'name="LoginForm\[password\]"', re.I)


class BackendError(RuntimeError):
    pass


class BackendLoginError(BackendError):
    pass


class BackendAdminClient:
    """單一後台管理員 session。建立後先 `login()` 再呼叫審核方法。"""

    def __init__(self, base: str, username: str, password: str):
        if not base:
            raise BackendError("後台 URL 未設定")
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.csrf_param = CSRF_PARAM
        self.csrf = ""
        self.session = requests.Session()
        # 內網/dev domain bypass 系統 proxy（Privoxy 會把 .worky.com.tw 導到正式站；沿用 client.py 慣例）
        self.session.trust_env = False
        self.session.proxies = {"http": "", "https": ""}
        # dev 後台走 https 但憑證自簽，關閉驗證（內網測試環境）
        self.session.verify = False
        try:
            from urllib3.exceptions import InsecureRequestWarning
            requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        except Exception:  # noqa: BLE001
            pass

    # ── 內部 ───────────────────────────────────────────────────────────────
    def _absorb_csrf(self, html: str) -> None:
        """從頁面 HTML 抓最新 CSRF token（meta 優先，否則隱藏欄位）。"""
        m = _META_PARAM.search(html or "")
        if m:
            self.csrf_param = m.group(1)
        m = _META_TOKEN.search(html or "") or _HIDDEN_TOKEN.search(html or "")
        if m:
            self.csrf = m.group(1)

    def _post(self, path: str, *, data: dict, params: dict | None = None,
              allow_redirects: bool = True) -> requests.Response:
        """帶 CSRF（表單欄位 + header 雙保險）的 POST。"""
        body = dict(data)
        body[self.csrf_param] = self.csrf
        return self.session.post(
            self.base + path, params=params, data=body,
            headers={"X-CSRF-Token": self.csrf, "X-Requested-With": "XMLHttpRequest"},
            timeout=30, allow_redirects=allow_redirects,
        )

    # ── 登入 ───────────────────────────────────────────────────────────────
    def login(self) -> None:
        if not self.username or not self.password:
            raise BackendLoginError("後台帳號或密碼未設定")
        try:
            r = self.session.get(self.base + "/site/login", timeout=30)
        except requests.RequestException as e:
            raise BackendLoginError(f"無法連線後台：{e}") from e
        self._absorb_csrf(r.text)
        if not self.csrf:
            raise BackendLoginError("登入頁取不到 CSRF token（後台 URL 可能不對）")

        # 不帶 X-Requested-With、且不自動跟隨：帶 AJAX 標頭時 Yii2 以 `X-Redirect`
        # （非 `Location`）回應，requests 不會跟隨、狀態停在 302 而被誤判失敗。
        # 改以「重定向目標」判定成敗：成功 → 導向首頁；失敗 → 重渲染 /site/login。
        body = {
            "LoginForm[username]": self.username,
            "LoginForm[password]": self.password,
            self.csrf_param: self.csrf,
        }
        try:
            resp = self.session.post(
                self.base + "/site/login", data=body,
                headers={"X-CSRF-Token": self.csrf},
                timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            raise BackendLoginError(f"無法連線後台：{e}") from e

        redirect = resp.headers.get("Location") or resp.headers.get("X-Redirect") or ""
        if resp.status_code in (301, 302) or redirect:
            if "/site/login" in redirect:
                raise BackendLoginError("帳號或密碼錯誤")
            # 成功導向首頁；再 GET 一次刷新 CSRF（審核方法也會各自重取，失敗不致命）
            try:
                home = self.session.get(redirect or self.base + "/", timeout=30)
                self._absorb_csrf(home.text)
            except requests.RequestException:
                pass
            return
        # 200：Yii2 重新渲染登入頁（仍含密碼欄位 / 錯誤訊息）
        text_ = resp.text
        if "Incorrect username or password" in text_:
            raise BackendLoginError("帳號或密碼錯誤")
        if _LOGIN_FORM.search(text_):
            raise BackendLoginError(f"登入未成功（HTTP {resp.status_code}）")
        self._absorb_csrf(text_)

    # ── 審核打工夥伴（labor id 為鍵；回 JSON code）──────────────────────────────
    def review_labor(self, labor_id: int, approve: bool,
                     reasons: dict | None = None) -> dict:
        if approve:
            resp = self._post("/labor/list/validate",
                              params={"id": labor_id},
                              data={"validateType": LABOR_VALIDATE_ENABLED})
        else:
            # reasons 形如 {typeValue: [reasonId,...]}；無指定時給一個泛用理由。
            # 重複鍵（reasons[tv][]）需用 list of tuples 表達。
            reasons = reasons or {"0": ["1"]}
            form = [("labor_id", str(labor_id))]
            for tv, ids in reasons.items():
                for rid in ids:
                    form.append((f"reasons[{tv}][]", str(rid)))
            form.append((self.csrf_param, self.csrf))
            resp = self.session.post(
                self.base + "/labor/list/rejection", data=form,
                headers={"X-CSRF-Token": self.csrf, "X-Requested-With": "XMLHttpRequest"},
                timeout=30)
        return self._parse_labor_result(resp)

    @staticmethod
    def _parse_labor_result(resp: requests.Response) -> dict:
        try:
            j = resp.json()
        except ValueError:
            raise BackendError(f"審核打工夥伴回應非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")
        code = j.get("code")
        if code != 0:
            raise BackendError(f"審核打工夥伴失敗：code={code} message={j.get('message')!r}")
        return {"ok": True, "message": j.get("message", "")}

    # ── 審核店鋪（shop id 為鍵；先鎖 REVIEWING 再 approve/reject；回 redirect）──────
    def review_shop(self, shop_id: int, approve: bool,
                    reason_ids: list | None = None, other_reason: str = "") -> dict:
        # 先 GET 審核頁：Shop::lockReviewing 把 SENT→REVIEWING，並取最新 csrf
        g = self.session.get(self.base + "/employer/shop/validation",
                             params={"id": shop_id}, timeout=30)
        self._absorb_csrf(g.text)

        # approve/reject 一律回 302（空 body），且帶 AJAX 標頭時 X-Redirect 會打回 approve
        # 本身造成迴圈，故不跟隨。動作成敗以 session flash 呈現在後續任一頁面。
        if approve:
            self._post("/employer/shop/validation/approve",
                       params={"id": shop_id},
                       data={"ShopValidationForm[shop_id]": shop_id},
                       allow_redirects=False)
        else:
            form = [("ShopValidationForm[shop_id]", str(shop_id))]
            for rid in (reason_ids or [1]):
                form.append(("ShopValidationForm[failed_reason_ids][]", str(rid)))
            if other_reason:
                form.append(("ShopValidationForm[other_failed_reason]", other_reason))
            form.append((self.csrf_param, self.csrf))
            self.session.post(
                self.base + "/employer/shop/validation/reject",
                params={"id": shop_id}, data=form,
                headers={"X-CSRF-Token": self.csrf, "X-Requested-With": "XMLHttpRequest"},
                timeout=30, allow_redirects=False)
        # GET 店鋪列表（穩定、不迴圈）讀取本次操作的 flash 判定成敗（每次審核皆新 session，無殘留）
        landing = self.session.get(self.base + "/employer/shop/list", timeout=30)
        return self._parse_shop_result(landing, approve)

    @staticmethod
    def _parse_shop_result(resp: requests.Response, approve: bool) -> dict:
        # 後台以 flash 提示成敗：成功 flash 為「<動作>執行成功」，失敗為「資料檢核失敗: …」或例外訊息。
        text_ = resp.text
        action = "通過審核" if approve else "駁回申請"
        if f"{action}執行成功" in text_ or "執行成功" in text_:
            return {"ok": True, "message": f"{action}執行成功"}
        # 失敗 flash：資料檢核失敗 / alert-danger 內文
        m = re.search(r"資料檢核失敗[:：]?\s*([^<\n]*)", text_)
        if m:
            raise BackendError(f"審核店鋪失敗：{m.group(0).strip()[:120]}")
        m = re.search(r'alert-danger[^>]*>\s*([^<]+?)\s*<', text_)
        if m:
            raise BackendError(f"審核店鋪失敗：{m.group(1).strip()[:120]}")
        raise BackendError("審核店鋪結果無法判定（後台未回傳成敗 flash）")
