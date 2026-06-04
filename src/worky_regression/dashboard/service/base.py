"""看板資料層共用基底：連線、白名單篩選、labor/employer/shop 名稱解析。

純讀取。複用 DBVerifier 的連線設定，不對主倉做任何寫入。各業務 mixin
（contract / jobs / manage / settings）共用此基底的 self.db / self.settings
與名稱解析 helper。
"""
from __future__ import annotations

from ...config import Settings
from ...verifier import DBVerifier


def apply_filters(where: list, params: list, filters: dict | None, allow: dict) -> None:
    """把白名單內的篩選條件加進 WHERE。allow={param_key:(column, mode)}，mode∈eq/min/max。

    只接受白名單欄位，值為空則略過——避免任意欄位注入。
    """
    if not filters:
        return
    for key, (col, mode) in allow.items():
        v = filters.get(key)
        if v in (None, ""):
            continue
        op = {"eq": "=", "min": ">=", "max": "<="}[mode]
        where.append(f"{col} {op} %s")
        params.append(v)


def num(v):
    """Decimal → float（JSON 可序列化）。"""
    if v is None:
        return None
    return float(v)


class ServiceBase:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.db = DBVerifier(self.settings)

    # ── labor 名稱解析（display_name 加密，只用 phone / username）─────────────
    def _labor_labels(self, ids: list[int]) -> dict[int, dict]:
        ids = [int(i) for i in {i for i in ids if i}]
        if not ids:
            return {}
        placeholders = ",".join(["%s"] * len(ids))
        rows = self.db.query_all(
            f"SELECT id, phone, username FROM s_labors WHERE id IN ({placeholders})",
            tuple(ids),
        )
        return {r["id"]: {"id": r["id"], "phone": r["phone"], "username": r["username"]}
                for r in rows}

    @staticmethod
    def _labor_label(labels: dict, uid) -> dict | None:
        if not uid:
            return None
        return labels.get(int(uid), {"id": int(uid), "phone": None, "username": None})

    def _emp_labels(self, ids: list[int]) -> dict[int, dict]:
        ids = [int(i) for i in {i for i in ids if i}]
        if not ids:
            return {}
        ph = ",".join(["%s"] * len(ids))
        rows = self.db.query_all(
            f"SELECT id, phone FROM s_employers WHERE id IN ({ph})", tuple(ids))
        return {r["id"]: {"id": r["id"], "phone": r["phone"]} for r in rows}

    def _shop_labels(self, ids: list[int]) -> dict[int, dict]:
        ids = [int(i) for i in {i for i in ids if i}]
        if not ids:
            return {}
        ph = ",".join(["%s"] * len(ids))
        rows = self.db.query_all(
            f"SELECT id, name, branch_name FROM s_shops WHERE id IN ({ph})", tuple(ids))
        return {r["id"]: {"id": r["id"], "name": r["name"], "branch_name": r["branch_name"]}
                for r in rows}
