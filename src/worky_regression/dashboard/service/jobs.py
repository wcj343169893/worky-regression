"""工作系統（job）：工作看板清單 / 詳情 / 統計。"""
from __future__ import annotations

from .. import status as st
from .base import num


class JobMixin:
    JOB_FILTERS = {"pay_status": ("j.pay_status", "eq"),
                   "payment_method_id": ("j.payment_method_id", "eq"),
                   "wage_min": ("j.hourly_wage", "min"), "wage_max": ("j.hourly_wage", "max")}

    def list_jobs(self, *, q: str = "", category: str | None = None,
                  filters: dict | None = None, limit: int = 50, offset: int = 0) -> dict:
        # Issue #1：全集 = 本框架已執行過、saved 留有 job_sn 的那批；不再全表掃描主倉。
        rows = self._executed_job_rows()

        if q:
            ql = q.lower()
            rows = [it for it in rows
                    if ql in (it["job_sn"] or "").lower()
                    or ql in (it["name"] or "").lower()]
        rows = self._apply_mem_filters(rows, filters, self.JOB_FILTERS)

        # category 篩選在分頁前套用（與 job_stats 同源、total/列數/分頁一致）。
        if category:
            rows = [it for it in rows if it["progress"]["category"] == category]

        total = len(rows)
        page = rows[int(offset):int(offset) + int(limit)]
        return {"total": total, "count": len(page), "limit": limit,
                "offset": offset, "items": page}

    def _executed_job_rows(self) -> list[dict]:
        """建出「已執行 job_sn 全集」對應的列（含主倉現況 + 降級列），供 list/stats 同源。"""
        executed = self.qa.executed_entities("job")
        if not executed:
            return []
        ex_by_sn = {e["sn"]: e for e in executed}
        ex_sns = list(ex_by_sn.keys())

        ph = ",".join(["%s"] * len(ex_sns))
        db_rows = self.db.query_all(
            f"""SELECT j.id, j.job_sn, j.custom_name, j.status, j.pay_status,
                       j.hourly_wage, j.estimated_total_amount, j.total_amount,
                       j.recruit_count, j.recruited_count, j.apply_count,
                       j.start_date, j.end_date, j.start_time_period, j.end_time_period,
                       j.start_at, j.end_at, j.recruit_deadline, j.payment_method_id,
                       j.employer_id, j.shop_id, j.city_id, j.district_id,
                       j.created_at, j.updated_at, j.published_at
                FROM s_jobs j
                WHERE j.is_deleted = 0 AND j.job_sn IN ({ph})
                ORDER BY j.id DESC""",
            tuple(ex_sns),
        )
        emps = self._emp_labels([r["employer_id"] for r in db_rows])
        shops = self._shop_labels([r["shop_id"] for r in db_rows])
        items: list[dict] = []
        found: set[str] = set()
        for r in db_rows:
            found.add(r["job_sn"])
            row = self._job_row(r, emps, shops)
            row["last_run"] = self._last_run(ex_by_sn.get(r["job_sn"]))
            items.append(row)

        degraded = [e for sn, e in ex_by_sn.items() if sn not in found]
        degraded.sort(key=lambda e: e["last_started_at"], reverse=True)
        for e in degraded:
            items.append(self._degraded_job_row(e))
        return items

    def _degraded_job_row(self, e: dict) -> dict:
        """主倉查無此 job_sn 時的降級列：以最後一次 run 狀態 / 時間呈現。"""
        return {
            "id": None, "job_sn": e["sn"], "name": "(僅記錄)",
            "status": None, "status_label": "-",
            "pay_status": None, "pay_status_label": "-",
            "hourly_wage": None, "estimated_total_amount": None, "total_amount": None,
            "recruit_count": None, "recruited_count": None, "apply_count": None,
            "start_date": None, "end_date": None,
            "start_time_period": None, "end_time_period": None,
            "start_at": None, "end_at": None,
            "payment_method_id": None, "payment_method_label": "-",
            "created_at": None, "updated_at": e["last_started_at"], "published_at": None,
            "city_id": None, "district_id": None,
            "employer": None, "shop": None,
            "progress": st.job_record_only_progress(),
            "last_run": self._last_run(e),
        }

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
        # Issue #1/#5：與 list_jobs 同源——同一批已執行列上算分布，total 必然一致。
        rows = self._executed_job_rows()
        by_cat: dict[str, int] = {}
        for it in rows:
            cat = it["progress"]["category"]
            by_cat[cat] = by_cat.get(cat, 0) + 1
        return {
            "total": len(rows),
            "active": by_cat.get("matching", 0) + by_cat.get("recruited", 0) + by_cat.get("running", 0),
            "completed": by_cat.get("done", 0),
            "canceled": by_cat.get("canceled", 0) + by_cat.get("failed", 0),
            "by_progress": [{"category": cat, "title": title, "count": by_cat.get(cat, 0)}
                            for cat, title in st.JOB_PROGRESS_ORDER],
        }
