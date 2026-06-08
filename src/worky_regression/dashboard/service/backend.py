"""後台管理員：帳密設定（qa_settings 持久化）+ 登入測試 + 審核打工夥伴/店鋪。

帳密由看板「系統設置」頁編輯並存進 qa_settings；base 缺時回退 .env 的 WORKY_BACKEND_BASE。
對外只回 password_set 布林、不外洩明文密碼。實際審核委派 BackendAdminClient。
"""
from __future__ import annotations

from ...backend_admin import BackendAdminClient, BackendError

_KEYS = ("backend_base", "backend_username", "backend_password")


class BackendMixin:
    # ── 設定讀寫 ───────────────────────────────────────────────────────────
    def backend_config(self) -> dict:
        cfg = self.qa.get_settings(list(_KEYS))
        return {
            "base": cfg.get("backend_base") or self.settings.backend_base or "",
            "username": cfg.get("backend_username") or "",
            "password_set": bool(cfg.get("backend_password")),
        }

    def update_backend_config(self, *, base: str | None = None,
                              username: str | None = None,
                              password: str | None = None) -> dict:
        items: dict[str, str] = {}
        if base is not None:
            items["backend_base"] = base.strip().rstrip("/")
        if username is not None:
            items["backend_username"] = username.strip()
        # 密碼留空 → 不覆蓋既有（前端不會回填明文）
        if password:
            items["backend_password"] = password
        self.qa.set_settings(items)
        return self.backend_config()

    # ── client 建立（內部）────────────────────────────────────────────────
    def _backend_client(self) -> BackendAdminClient:
        cfg = self.qa.get_settings(list(_KEYS))
        base = cfg.get("backend_base") or self.settings.backend_base
        return BackendAdminClient(
            base=base or "",
            username=cfg.get("backend_username") or "",
            password=cfg.get("backend_password") or "",
        )

    # ── 登入測試 ───────────────────────────────────────────────────────────
    def backend_login_test(self) -> dict:
        try:
            self._backend_client().login()
            return {"ok": True, "message": "登入成功"}
        except BackendError as e:
            return {"ok": False, "message": str(e)}

    # ── 審核（建 client → login → 審核）────────────────────────────────────
    def review_labor(self, labor_id: int, approve: bool,
                     reasons: dict | None = None) -> dict:
        client = self._backend_client()
        client.login()
        return client.review_labor(int(labor_id), approve, reasons=reasons)

    def review_shop(self, shop_id: int, approve: bool,
                    reason_ids: list | None = None, other_reason: str = "") -> dict:
        client = self._backend_client()
        client.login()
        return client.review_shop(int(shop_id), approve,
                                  reason_ids=reason_ids, other_reason=other_reason)
