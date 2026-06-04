"""工作系統（job）：工作看板清單 / 詳情 / 統計。"""
from __future__ import annotations

from .. import status as st
from .base import apply_filters, num


class JobMixin:
    JOB_FILTERS = {"pay_status": ("j.pay_status", "eq"),
                   "payment_method_id": ("j.payment_method_id", "eq"),
                   "wage_min": ("j.hourly_wage", "min"), "wage_max": ("j.hourly_wage", "max")}

    def list_jobs(self, *, q: str = "", category: str | None = None,
                  filters: dict | None = None, limit: int = 50, offset: int = 0) -> dict:
        where = ["j.is_deleted = 0"]
        params: list = []
        if q:
            where.append("(j.job_sn LIKE %s OR j.custom_name LIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
        if category and category in st.CATEGORY_STATUSES:
            vals = st.CATEGORY_STATUSES[category]
            where.append(f"j.status IN ({','.join(['%s'] * len(vals))})")
            params += vals
        apply_filters(where, params, filters, self.JOB_FILTERS)
        where_sql = " AND ".join(where)
        total = self.db.query_one(
            f"SELECT COUNT(*) c FROM s_jobs j WHERE {where_sql}", tuple(params))["c"]
        rows = self.db.query_all(
            f"""SELECT j.id, j.job_sn, j.custom_name, j.status, j.pay_status,
                       j.hourly_wage, j.estimated_total_amount, j.total_amount,
                       j.recruit_count, j.recruited_count, j.apply_count,
                       j.start_date, j.end_date, j.start_time_period, j.end_time_period,
                       j.start_at, j.end_at, j.recruit_deadline, j.payment_method_id,
                       j.employer_id, j.shop_id, j.city_id, j.district_id,
                       j.created_at, j.updated_at, j.published_at
                FROM s_jobs j WHERE {where_sql}
                ORDER BY j.id DESC LIMIT %s OFFSET %s""",
            tuple(params) + (int(limit), int(offset)),
        )
        emps = self._emp_labels([r["employer_id"] for r in rows])
        shops = self._shop_labels([r["shop_id"] for r in rows])
        items = [self._job_row(r, emps, shops) for r in rows]
        return {"total": total, "count": len(items), "limit": limit,
                "offset": offset, "items": items}

    def _job_row(self, r: dict, emps: dict, shops: dict) -> dict:
        return {
            "id": r["id"], "job_sn": r["job_sn"],
            "name": r["custom_name"],
            "status": r["status"], "status_label": st.label(st.JOB_STATUS, r["status"]),
            "pay_status": r["pay_status"],
            "pay_status_label": st.label(st.JOB_PAY_STATUS, r["pay_status"]),
            "hourly_wage": num(r["hourly_wage"]),
            "estimated_total_amount": num(r["estimated_total_amount"]),
            "total_amount": num(r["total_amount"]),
            "recruit_count": r["recruit_count"], "recruited_count": r["recruited_count"],
            "apply_count": r["apply_count"],
            "start_date": r["start_date"], "end_date": r["end_date"],
            "start_time_period": r["start_time_period"], "end_time_period": r["end_time_period"],
            "start_at": r["start_at"], "end_at": r["end_at"],
            "payment_method_id": r["payment_method_id"],
            "payment_method_label": st.label(st.PAYMENT_METHOD, r["payment_method_id"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "published_at": r["published_at"],
            "city_id": r["city_id"], "district_id": r["district_id"],
            "employer": emps.get(int(r["employer_id"])) if r["employer_id"] else None,
            "shop": shops.get(int(r["shop_id"])) if r["shop_id"] else None,
            "progress": st.job_progress(r["status"]),
        }

    def job_detail(self, job_sn: str) -> dict | None:
        j = self.db.query_one("SELECT * FROM s_jobs WHERE job_sn = %s", (job_sn,))
        if not j:
            return None
        jid = j["id"]
        labor_jobs = self.db.query_all(
            """SELECT id, labor_id, status, job_status, agree_to_work, wage, total_wage,
                      start_at, end_at, start_code, end_code,
                      actual_clock_in_at, actual_clock_out_at, work_minutes,
                      canceled_at, canceled_reason_id, created_at, updated_at
               FROM s_labor_jobs WHERE job_id = %s ORDER BY id""", (jid,))
        match_jobs = self.db.query_all(
            """SELECT id, labor_id, type, status, `read`, displayed,
                      created_at, updated_at, employer_declined_at
               FROM s_labor_match_jobs WHERE job_id = %s ORDER BY id""", (jid,))
        emps = self._emp_labels([j["employer_id"]])
        shops = self._shop_labels([j["shop_id"]])
        labels = self._labor_labels(
            [r["labor_id"] for r in labor_jobs] + [r["labor_id"] for r in match_jobs])
        return {
            "job": self._job_row(j, emps, shops),
            "address": j.get("address"),
            "labor_jobs": [
                {"id": r["id"], "labor": self._labor_label(labels, r["labor_id"]),
                 "status": r["status"], "status_label": st.label(st.LABOR_JOB_STATUS, r["status"]),
                 "job_status": r["job_status"],
                 "job_status_label": st.label(st.LABOR_JOB_JOB_STATUS, r["job_status"]),
                 "agree_to_work": r["agree_to_work"], "wage": num(r["wage"]),
                 "total_wage": num(r["total_wage"]),
                 "start_at": r["start_at"], "end_at": r["end_at"],
                 "start_code": r["start_code"], "end_code": r["end_code"],
                 "actual_clock_in_at": r["actual_clock_in_at"],
                 "actual_clock_out_at": r["actual_clock_out_at"],
                 "work_minutes": r["work_minutes"], "canceled_at": r["canceled_at"],
                 "created_at": r["created_at"], "updated_at": r["updated_at"]}
                for r in labor_jobs
            ],
            "match_jobs": [
                {"id": r["id"], "labor": self._labor_label(labels, r["labor_id"]),
                 "type": r["type"], "status": r["status"],
                 "status_label": st.label(st.LABOR_MATCH_STATUS, r["status"]),
                 "read": r["read"], "displayed": r["displayed"],
                 "created_at": r["created_at"], "updated_at": r["updated_at"]}
                for r in match_jobs
            ],
        }

    def job_stats(self) -> dict:
        rows = self.db.query_all(
            "SELECT status, COUNT(*) c FROM s_jobs WHERE is_deleted = 0 GROUP BY status")
        by_cat: dict[str, int] = {}
        total = 0
        for r in rows:
            total += r["c"]
            cat = st.JOB_PROGRESS.get(int(r["status"]), ("draft", ""))[0]
            by_cat[cat] = by_cat.get(cat, 0) + r["c"]
        return {
            "total": total,
            "active": by_cat.get("matching", 0) + by_cat.get("recruited", 0) + by_cat.get("running", 0),
            "completed": by_cat.get("done", 0),
            "canceled": by_cat.get("canceled", 0) + by_cat.get("failed", 0),
            "by_progress": [{"category": cat, "title": title, "count": by_cat.get(cat, 0)}
                            for cat, title in st.JOB_PROGRESS_ORDER],
        }
