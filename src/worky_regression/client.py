"""Worky API HTTP client：簽名、headers、token 管理。

簽名規則 (見 /www/wwwroot/worky/documents/api/001-API說明.md)：
    md5(
        urlQueryString_sorted    # GET 才有，POST 留空
        + postBody_json          # POST 才有，trimmed
        + xWorkyCommonVariables  # 永遠都帶
        + accessToken            # 匿名接口留空
        + apiSecret
    )
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import requests

from .config import Settings


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class WorkyClient:
    """單一 actor 的 HTTP client。`user_type`=1 employer / 2 labor。"""

    def __init__(self, settings: Settings, user_type: int):
        self.settings = settings
        self.user_type = user_type
        # 主 API base/secret 按 user_type 分流（employer 可走 /qa-v1，labor 走 /v1）；
        # qa_mode＝本 client 的主 base 是 QA 專用前綴（決定是否自動補 shop_id）。
        self.api_base = settings.api_base_for(user_type)
        self.api_secret = settings.api_secret_for(user_type)
        self.qa_mode = "qa-v1" in self.api_base
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.access_token_expired_at: int = 0
        self.refresh_token_expired_at: int = 0
        self.udid = uuid.uuid4().hex  # 32 chars
        # employer 鎖定店鋪用（qa-v1 限定）：設定後，已登入的主 API 請求未帶 shop_id 時自動補上，
        # 後端 QA 模式會以此覆寫 lastSelectedShopId，查驗不受其他端「切換店鋪」影響。
        self.shop_id: int | None = None
        self.session = requests.Session()
        # 內網/dev domain bypass 系統 proxy（Privoxy 會吞 .worky.com.tw）
        self.session.trust_env = False

    def _common_vars_json(self) -> str:
        """X-Worky-Common-Variables header value（JSON，無多餘空白）。"""
        payload = {
            "platform": self.settings.platform,
            "sdkVersion": self.settings.sdk_version,
            "userType": self.user_type,
            "udid": self.udid,
            "deviceName": self.settings.device_name,
            "time": int(time.time()),
        }
        # ensure_ascii=False 與 PHP json_encode 預設一致；separators 去除多餘空白
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _signature(self, query_string: str, body_str: str, common_vars: str,
                   secret: str | None = None) -> str:
        return md5(query_string + body_str + common_vars + self.access_token
                   + (secret if secret is not None else self.api_secret))

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, base: str | None = None,
                _retried: bool = False) -> requests.Response:
        # base 可覆寫 API base（如營運活動走 /activity；不傳則用本 client 的主 base）。
        # 覆寫 base 的請求（activity 模組）簽名用預設 secret，不用 employer 分流的 qa-v1 secret。
        # 長用例（打卡類要等 60+ 分鐘）執行途中 access token 會過期（TTL≈1h）：
        # 請求前主動檢查，快過期且可刷新就先刷新（刷新端點自身除外，避免遞迴）。
        if (path != "/token/refresh" and self.access_token
                and not self.access_valid() and self.refresh_valid()):
            self.refresh()
        url = (base or self.api_base) + path
        secret = self.settings.api_secret if base is not None else self.api_secret

        # qa-v1 鎖店：employer 已登入、打主 API 且呼叫端沒給 shop_id 時自動補
        # （顯式給的 shop_id 不覆寫——負向用例要能帶錯誤店鋪；activity 等覆寫 base 的請求不補）
        if (self.shop_id and self.user_type == 1 and base is None
                and self.access_token and self.qa_mode):
            if method.upper() == "GET":
                params = {"shop_id": self.shop_id, **(params or {})}
            else:
                body = {"shop_id": self.shop_id, **(body or {})}

        # GET query string: 參數名稱正序排列
        if method.upper() == "GET" and params:
            sorted_params = dict(sorted(params.items()))
            query_string = urlencode(sorted_params)
            url = f"{url}?{query_string}"
        else:
            query_string = ""

        # POST body: JSON 字串，去前後空白；空值用 "{}"
        if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            body_str = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":"))
        else:
            body_str = ""

        common_vars = self._common_vars_json()
        sig = self._signature(query_string, body_str, common_vars, secret=secret)

        headers = {
            "Content-Type": "application/json",
            "X-Worky-Common-Variables": common_vars,
            "X-Worky-Signature": sig,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        resp = self.session.request(
            method=method,
            url=url,
            data=body_str if body_str else None,
            headers=headers,
            timeout=30,
        )
        # 兜底：仍收到 10003 AccessToken 已失效（expired_at 不可靠 / 被踢下線）
        # → 就地刷新一次，用新 token 重簽重送；只重試一次，避免循環。
        if (not _retried and path != "/token/refresh"
                and self._token_expired_resp(resp) and self.refresh()):
            return self.request(method, path, params=params, body=body, base=base, _retried=True)
        return resp

    @staticmethod
    def _token_expired_resp(resp: requests.Response) -> bool:
        """回應是否為「10003 AccessToken 已失效」業務錯（HTTP 200 + success=false）。"""
        if not resp.headers.get("content-type", "").startswith("application/json"):
            return False
        try:
            p = resp.json()
        except ValueError:
            return False
        return p.get("success") is False and int(p.get("code") or 0) == 10003

    def post(self, path: str, body: dict | None = None) -> requests.Response:
        return self.request("POST", path, body=body)

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return self.request("GET", path, params=params)

    def upload_file(self, file_type: str, content: bytes, *, filename: str = "upload.png",
                    content_type: str = "image/png", response_type: int = 2) -> requests.Response:
        """上傳檔案（multipart）到 /file-upload。回 requests.Response（data.uploadedFiles 為網址陣列）。

        簽名規則與 JSON 不同（見 worky api/components/Request.php）：Content-Type 非 application/json
        時 rawBody 為空 → 改用表單欄位，且 **ksort 排序** 後 http_build_query，再接 commonVars/token/secret。
        檔案本身不參與簽名。故不可走 self.request（那會帶 application/json）。
        """
        fields = {"type": file_type, "response_type": str(response_type)}
        common_vars = self._common_vars_json()
        # ksort：key 升序後 urlencode（http_build_query 等價），multipart 無 JSON body
        sig_str = urlencode(dict(sorted(fields.items())))
        sig = md5(sig_str + common_vars + self.access_token + self.api_secret)
        headers = {"X-Worky-Common-Variables": common_vars, "X-Worky-Signature": sig}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return self.session.post(
            self.api_base + "/file-upload",
            data=fields, files={"files": (filename, content, content_type)},
            headers=headers, timeout=30)

    def set_access_token(self, token: str, expired_at: int = 0,
                          refresh_token: str = "", refresh_expired_at: int = 0) -> None:
        self.access_token = token
        self.access_token_expired_at = expired_at
        self.refresh_token = refresh_token
        self.refresh_token_expired_at = refresh_expired_at

    # ── token 生命週期 ────────────────────────────────────────────────────────
    def access_valid(self, buffer_secs: int = 300) -> bool:
        """access token 是否仍可用（預留 buffer 秒緩衝，避免請求途中剛好過期）。

        expired_at=0 視為「不知道過期時間」→ 當作無效，逼呼叫端走刷新/登入較安全。
        """
        if not self.access_token or self.access_token_expired_at <= 0:
            return False
        return int(time.time()) + buffer_secs < self.access_token_expired_at

    def refresh_valid(self, buffer_secs: int = 0) -> bool:
        """refresh token 是否仍可用（refresh 有效期 30 天，buffer 預設 0）。"""
        if not self.refresh_token or self.refresh_token_expired_at <= 0:
            return False
        return int(time.time()) + buffer_secs < self.refresh_token_expired_at

    def refresh(self) -> bool:
        """用 refresh token 換新 access token（POST /token/refresh，不需帶 access token）。

        成功則更新 access/refresh token 與兩個過期時間並回 True；無 refresh token 或
        後端拒絕則回 False（呼叫端據此 fallback 完整登入）。
        """
        if not self.refresh_token:
            return False
        # 刷新端點不需 access token，且舊 access token 可能已過期；簽名時清掉以免污染。
        saved = self.access_token
        self.access_token = ""
        try:
            resp = self.post("/token/refresh", body={"refreshToken": self.refresh_token})
        except requests.RequestException:
            self.access_token = saved
            return False
        if resp.status_code != 200:
            self.access_token = saved
            return False
        payload = resp.json()
        if payload.get("success") is False:
            self.access_token = saved
            return False
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            self.access_token = saved
            return False
        # 刷新端點回 accessTokenExpired / refreshTokenExpired；登入端點回 ...ExpiredAt。兩種都收。
        self.set_access_token(
            token=token,
            expired_at=data.get("accessTokenExpired") or data.get("accessTokenExpiredAt") or 0,
            refresh_token=data.get("refreshToken") or self.refresh_token,
            refresh_expired_at=data.get("refreshTokenExpired") or data.get("refreshTokenExpiredAt") or 0,
        )
        return True
