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

    def register_accounts(self, role: str, n: int) -> dict:
        """純 API 自助建 n 個帳號入池（產 09 手機號 → 註冊 → 補資料）。回成功數 + 逐筆結果。"""
        if role not in ("labor", "employer"):
            raise ValueError("role 只能 labor / employer")
        n = max(1, min(int(n), 20))   # 上限保護：單次最多 20，避免誤觸大量註冊
        results = self._pool().register_via_api(role, n)
        return {"ok": sum(1 for r in results if r.get("ok")), "total": len(results), "results": results}

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
            actor.login(audit_code=s.audit_sms_code)
            return {"ok": True, "account_id": account_id, "message": f"登入成功（user_id={account_id}）"}
        except LoginFailedError as e:
            return {"ok": False, "account_id": account_id, "message": str(e)}
        except Exception as e:  # noqa: BLE001 — 任何登入失敗都回給前端顯示，不拋 500
            return {"ok": False, "account_id": account_id, "message": f"{type(e).__name__}: {e}"}
