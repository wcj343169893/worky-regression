"""帳號池管理（labor / employer）：檢視 qa_accounts、測試登入、啟用/停用。

只讀寫 QA 庫（qa_accounts），不碰被測後端 DB；測試登入是打被測 **API**（/labor|/employer/login），
正好用來診斷「某帳號登入報錯」。執行期配發走 AccountPool.acquire（caps + LRU 輪換）。
"""
from __future__ import annotations

from ...actor import Actor, LoginFailedError
from ...client import WorkyClient
from ...qa_accounts import AccountPool


class AccountsMixin:
    def _pool(self) -> AccountPool:
        return AccountPool(self.settings)

    def list_accounts(self) -> dict:
        """回傳池內帳號（依角色分組）+ 各角色「可配發(available 且具完整 caps)」數，供前端標示可換號性。"""
        rows = self._pool().list_pool()
        by_role: dict[str, list] = {}
        for r in rows:
            by_role.setdefault(r["role"], []).append(r)
        groups = [{
            "role": role,
            "count": len(items),
            "available": sum(1 for x in items if x["state"] == "available"),
            "items": items,
        } for role, items in sorted(by_role.items())]
        return {"groups": groups, "total": len(rows)}

    # 後台核准才能達成的能力（其餘 active/clean/profile_complete/verified_shop 純 API 即可）
    _APPROVE_CAPS = {"verified", "shop_approved"}
    # 各角色「依能力初始化」的目標分群（每群建 per_cap 個）；audit_role 走 provision 種子，不在此
    _INIT_TARGETS = {
        "labor": [["active", "clean"], ["verified"], ["profile_complete"]],
        "employer": [["active"], ["shop_approved"], ["verified_shop"]],
    }

    def register_accounts(self, role: str, n: int, *, caps: list[str] | None = None,
                          auto_review: bool = True) -> dict:
        """純 API 自助建 n 個帳號入池（產 09 手機號 → 註冊 → 補資料）。回成功數 + 逐筆結果。

        caps＝**目標能力**：決定 API 端步驟（labor profile_complete→完整送審、employer verified_shop→type2），
        以及是否需後台核准（caps 含 verified/shop_approved 才核准）。caps 省略＝舊行為（基本資料 +
        auto_review 旗標決定是否核准）。後台帳密未設 / 登入失敗則跳過核准、照常入池。
        """
        if role not in ("labor", "employer"):
            raise ValueError("role 只能 labor / employer")
        n = max(1, min(int(n), 20))   # 上限保護：單次最多 20，避免誤觸大量註冊
        results = self._pool().register_via_api(role, n, caps=caps)
        # 是否需核准：指定 caps 時看 caps；未指定(舊呼叫) 時看 auto_review 旗標
        need_approve = (auto_review if caps is None
                        else bool(self._APPROVE_CAPS & set(caps)))
        reviewed = 0
        if need_approve:
            self.auto_review_registered(results)   # 就地補上每筆 review / 最新 caps
            reviewed = sum(1 for r in results if r.get("review") == "approved")
        # 以工作庫實況重探每筆 caps（employer verified_shop/shop_approved、labor profile_complete/verified
        # 都在工作庫，profile 端看不到）→ 確保入池 caps 名副其實，不論是否核准
        for r in results:
            if r.get("ok"):
                synced = self._sync_caps_safe(int(r["account_id"]), role)
                if isinstance(synced, dict) and isinstance(synced.get("caps"), list):
                    r["caps"] = synced["caps"]
        return {"ok": sum(1 for r in results if r.get("ok")), "total": len(results),
                "reviewed": reviewed, "auto_review": need_approve, "caps": caps, "results": results}

    def init_pool(self, *, per_cap: int = 3) -> dict:
        """**全清重建**帳號池：清空當前庫池列 → provision 種子(補 audit_role 等純 API 達不到的) →
        按能力分群各建 per_cap 個（labor: active+clean / verified / profile_complete；
        employer: active / shop_approved / verified_shop）。回各步驟摘要。

        注意：會送出大量註冊/上傳/核准請求，耗時較長（同步執行）。後台帳密未設則核准類自動跳過。
        """
        per_cap = max(1, min(int(per_cap), 10))
        pool = self._pool()
        cleared = pool.clear()                       # 全清（labor+employer）
        try:
            provisioned = pool.provision()           # 種子硬狀態校正 + 能力探測（含 audit_role）
        except Exception as e:  # noqa: BLE001 — provision 失敗不應中斷整個初始化
            provisioned = {"error": f"{type(e).__name__}: {e}"}
        groups: list[dict] = []
        for role, targets in self._INIT_TARGETS.items():
            for tcaps in targets:
                r = self.register_accounts(role, per_cap, caps=tcaps)
                groups.append({"role": role, "target_caps": tcaps,
                               "ok": r["ok"], "total": r["total"], "reviewed": r["reviewed"]})
        return {"cleared": cleared, "provisioned": provisioned, "per_cap": per_cap,
                "groups": groups}

    def set_account_state(self, account_id: int, role: str, state: str) -> dict:
        updated = self._pool().set_state(account_id, role, state)
        if not updated:
            raise ValueError(f"帳號池無 {role} id={account_id}")
        return {"account_id": account_id, "role": role, "state": state}

    def account_test_login(self, account_id: int, role: str) -> dict:
        """以該池帳號實際打被測登入 API，回 ok/訊息（診斷登入報錯用）。不寫任何狀態。"""
        row = self._pool().get(account_id, role)
        if row is None:
            raise ValueError(f"帳號池無 {role} id={account_id}")
        s = self.settings
        client = WorkyClient(s, user_type=row["user_type"])
        actor = Actor(role=role, user_type=row["user_type"], phone=row["phone"],
                      user_id=row["account_id"], client=client, shop_id=row.get("shop_id"))
        try:
            actor.login()   # 統一走真實「發碼→確認」，不用固定碼
            return {"ok": True, "account_id": account_id, "message": f"登入成功（user_id={account_id}）"}
        except LoginFailedError as e:
            return {"ok": False, "account_id": account_id, "message": str(e)}
        except Exception as e:  # noqa: BLE001 — 任何登入失敗都回給前端顯示，不拋 500
            return {"ok": False, "account_id": account_id, "message": f"{type(e).__name__}: {e}"}
