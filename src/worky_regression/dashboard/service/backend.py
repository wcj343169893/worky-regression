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

    def _logged_in_client(self) -> tuple[BackendAdminClient | None, str | None]:
        """建 client 並登入一次；成功回 (client, None)，未設帳密/連不上/登入失敗回 (None, 原因)。

        供批次自動審核共用：只檢查一次可用性、之後重用同一 session（免每筆重新登入）。
        """
        try:
            client = self._backend_client()
            client.login()
            return client, None
        except BackendError as e:   # 含 BackendLoginError（帳密未設 / 帳密錯）
            return None, str(e)

    # ── 登入測試 ───────────────────────────────────────────────────────────
    def backend_login_test(self) -> dict:
        try:
            self._backend_client().login()
            return {"ok": True, "message": "登入成功"}
        except BackendError as e:
            return {"ok": False, "message": str(e)}

    # ── 審核（建 client → login → 審核 → 池內帳號重探 caps）────────────────────
    def review_labor(self, labor_id: int, approve: bool,
                     reasons: dict | None = None) -> dict:
        client = self._backend_client()
        client.login()
        return self._do_review_labor(client, int(labor_id), approve, reasons=reasons)

    def review_shop(self, shop_id: int, approve: bool,
                    reason_ids: list | None = None, other_reason: str = "") -> dict:
        client = self._backend_client()
        client.login()
        return self._do_review_shop(client, int(shop_id), approve,
                                    reason_ids=reason_ids, other_reason=other_reason)

    # ── 審核核心（已登入 client；公開方法與批次自動審核共用，免重複登入）──────────
    def _do_review_labor(self, client: BackendAdminClient, labor_id: int, approve: bool,
                         reasons: dict | None = None) -> dict:
        result = client.review_labor(labor_id, approve, reasons=reasons)
        # 審核改了工作庫硬狀態 → 若該帳號在池中，重探重算 caps（labor 通過→補 verified）
        result["caps_synced"] = self._sync_caps_safe(labor_id, "labor")
        return result

    def _do_review_shop(self, client: BackendAdminClient, shop_id: int, approve: bool,
                        reason_ids: list | None = None, other_reason: str = "") -> dict:
        result = client.review_shop(shop_id, approve,
                                    reason_ids=reason_ids, other_reason=other_reason)
        # 店鋪歸屬商家：若在池中，重探重算 caps（通過→補 shop_approved）
        result["caps_synced"] = self._sync_shop_owner_caps_safe(shop_id)
        return result

    # ── 批次自動審核（註冊建號順帶通過：labor→驗證、employer→店鋪核准）────────────
    def auto_review_registered(self, results: list[dict]) -> list[dict]:
        """對 register_via_api 的成功結果自動審核通過，就地補上 review/caps 後回傳 results。

        後台帳密未設 / 連不上 / 登入失敗 → 全部跳過（標 review='skipped:<原因>'），帳號照常留池。
        單筆審核失敗只標該筆 review='failed:<原因>'，不影響其餘。整批共用一個已登入 session。
        """
        oks = [r for r in results if r.get("ok")]
        if not oks:
            return results
        client, reason = self._logged_in_client()
        if client is None:
            for r in oks:
                r["review"] = f"skipped:{reason}"
            return results
        for r in oks:
            try:
                if r.get("role") == "labor":
                    res = self._do_review_labor(client, int(r["account_id"]), True)
                else:
                    sid = r.get("shop_id")
                    if not sid:
                        r["review"] = "skipped:無 shop_id（店鋪未建成）"
                        continue
                    res = self._do_review_shop(client, int(sid), True)
                r["review"] = "approved"
                synced = res.get("caps_synced")
                if isinstance(synced, dict) and isinstance(synced.get("caps"), list):
                    r["caps"] = synced["caps"]   # 以重探後的最新 caps 覆蓋（含 verified/shop_approved）
            except BackendError as e:
                r["review"] = f"failed:{e}"
        return results

    # ── caps 重探（best-effort：失敗不影響審核結果回報）────────────────────────
    def _sync_caps_safe(self, account_id: int, role: str):
        from ...qa_accounts import AccountPool
        try:
            return AccountPool(self.settings).sync_account_caps(account_id, role)
        except Exception as e:  # noqa: BLE001 — 重探失敗不可吃掉審核成功的結果
            return {"error": f"{type(e).__name__}: {e}"}

    def _sync_shop_owner_caps_safe(self, shop_id: int):
        from ...qa_accounts import AccountPool
        try:
            return AccountPool(self.settings).resync_shop_owner(shop_id)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
