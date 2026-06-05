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
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.access_token_expired_at: int = 0
        self.udid = uuid.uuid4().hex  # 32 chars
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

    def _signature(self, query_string: str, body_str: str, common_vars: str) -> str:
        return md5(query_string + body_str + common_vars + self.access_token + self.settings.api_secret)

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, base: str | None = None) -> requests.Response:
        # base 可覆寫 API base（如營運活動走 /activity；不傳則用主 API /v1）
        url = (base or self.settings.api_base) + path

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
        sig = self._signature(query_string, body_str, common_vars)

        headers = {
            "Content-Type": "application/json",
            "X-Worky-Common-Variables": common_vars,
            "X-Worky-Signature": sig,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        return self.session.request(
            method=method,
            url=url,
            data=body_str if body_str else None,
            headers=headers,
            timeout=30,
        )

    def post(self, path: str, body: dict | None = None) -> requests.Response:
        return self.request("POST", path, body=body)

    def get(self, path: str, params: dict | None = None) -> requests.Response:
        return self.request("GET", path, params=params)

    def set_access_token(self, token: str, expired_at: int = 0,
                          refresh_token: str = "") -> None:
        self.access_token = token
        self.access_token_expired_at = expired_at
        self.refresh_token = refresh_token
