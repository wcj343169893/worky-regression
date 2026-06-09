"""管理清單：打工夥伴 / 商家 / 店鋪（純清單，display_name 加密故以 phone/id 呈現）。"""
from __future__ import annotations

from .base import apply_filters


class ManageMixin:
    LABOR_FILTERS = {"status": ("status", "eq"), "valid_status": ("valid_status", "eq"),
                     "is_profile_complete": ("is_profile_complete", "eq")}

    def list_labors(self, *, q: str = "", filters: dict | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if q:
            where.append("(phone LIKE %s OR username LIKE %s OR CAST(id AS CHAR)=%s)")
            params += [f"%{q}%", f"%{q}%", q]
        apply_filters(where, params, filters, self.LABOR_FILTERS)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) c FROM s_labors {wsql}", tuple(params))["c"]
        rows = self.db.query_all(
            f"""SELECT id, username, phone, status, valid_status, is_profile_complete,
                       rating_stars, evaluation_count, job_count, canceled_count,
                       no_show_count, late_count, penalty_points,
                       last_login_at, created_at
                FROM s_labors {wsql} ORDER BY id DESC LIMIT %s OFFSET %s""",
            tuple(params) + (int(limit), int(offset)))
        return {"total": total, "count": len(rows), "limit": limit, "offset": offset,
                "items": rows}

    EMPLOYER_FILTERS = {"status": ("status", "eq"),
                        "is_payment_locked": ("is_payment_locked", "eq")}

    def list_employers(self, *, q: str = "", filters: dict | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if q:
            where.append("(phone LIKE %s OR CAST(id AS CHAR)=%s)")
            params += [f"%{q}%", q]
        apply_filters(where, params, filters, self.EMPLOYER_FILTERS)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) c FROM s_employers {wsql}", tuple(params))["c"]
        rows = self.db.query_all(
            f"""SELECT id, username, phone, status, shop_count, shop_upper_limit,
                       is_payment_locked, payment_failed_count, last_login_at, created_at
                FROM s_employers {wsql} ORDER BY id DESC LIMIT %s OFFSET %s""",
            tuple(params) + (int(limit), int(offset)))
        return {"total": total, "count": len(rows), "limit": limit, "offset": offset,
                "items": rows}

    SHOP_FILTERS = {"validation_status": ("validation_status", "eq"),
                    "validation_type": ("validation_type", "eq"),
                    "employer_id": ("employer_id", "eq")}

    def list_shops(self, *, q: str = "", filters: dict | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if q:
            where.append("(name LIKE %s OR branch_name LIKE %s OR CAST(id AS CHAR)=%s)")
            params += [f"%{q}%", f"%{q}%", q]
        apply_filters(where, params, filters, self.SHOP_FILTERS)
        wsql = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) c FROM s_shops {wsql}", tuple(params))["c"]
        rows = self.db.query_all(
            f"""SELECT id, name, branch_name, employer_id, city, district,
                       validation_type, validation_status, job_count, published_job_count,
                       canceled_count, rating_stars, evaluation_count, created_at
                FROM s_shops {wsql} ORDER BY id DESC LIMIT %s OFFSET %s""",
            tuple(params) + (int(limit), int(offset)))
        return {"total": total, "count": len(rows), "limit": limit, "offset": offset,
                "items": rows}
