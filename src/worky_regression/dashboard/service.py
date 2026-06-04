"""看板資料層：查 worky DB 組出任務清單 / 任務詳情 / 統計。

純讀取。複用 DBVerifier 的連線設定，不對主倉做任何寫入。
"""
from __future__ import annotations

import time

from ..config import Settings
from ..verifier import DBVerifier
from . import status as st


def _apply_filters(where: list, params: list, filters: dict | None, allow: dict) -> None:
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


class DashboardService:
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

    # ── 任務清單 ───────────────────────────────────────────────────────────
    TASK_FILTERS = {"pay_status": ("t.pay_status", "eq"),
                    "payment_method_id": ("t.payment_method_id", "eq")}

    def list_tasks(self, *, q: str = "", progress: int | None = None,
                   publisher_id: int | None = None, filters: dict | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        where = ["t.is_deleted = 0"]
        params: list = []
        if q:
            where.append("(t.task_sn LIKE %s OR t.name LIKE %s)")
            params += [f"%{q}%", f"%{q}%"]
        if publisher_id:
            where.append("t.created_by = %s")
            params.append(int(publisher_id))
        _apply_filters(where, params, filters, self.TASK_FILTERS)
        where_sql = " AND ".join(where)

        # 取每個任務「最新一筆」接案者任務的 task_status，用來推導進度
        base = f"""
            FROM s_contract_tasks t
            LEFT JOIN (
                SELECT rt.task_id, rt.task_status, rt.status AS rstatus, rt.receiver_id
                FROM s_contract_receiver_tasks rt
                JOIN (SELECT task_id, MAX(id) mid
                      FROM s_contract_receiver_tasks GROUP BY task_id) m
                  ON rt.id = m.mid
            ) prt ON prt.task_id = t.id
            WHERE {where_sql}
        """
        total = self.db.query_one(f"SELECT COUNT(*) c {base}", tuple(params))["c"]

        rows = self.db.query_all(
            f"""
            SELECT t.id, t.task_sn, t.name, t.status, t.pay_status, t.task_amount,
                   t.estimated_total_amount, t.total_amount, t.start_at, t.end_at,
                   t.recruit_count, t.recruited_count, t.recruit_deadline,
                   t.payment_method_id, t.created_at, t.updated_at, t.published_at,
                   t.created_by, t.city_id, t.district_id,
                   prt.task_status AS receiver_task_status, prt.receiver_id
            {base}
            ORDER BY t.id DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params) + (int(limit), int(offset)),
        )

        now = int(time.time())
        labels = self._labor_labels([r["created_by"] for r in rows]
                                    + [r["receiver_id"] for r in rows])
        items = []
        for r in rows:
            prog = st.derive_progress(
                task_status=r["status"], pay_status=r["pay_status"],
                recruit_deadline=r["recruit_deadline"],
                receiver_task_status=r["receiver_task_status"], now=now,
            )
            items.append(self._task_row(r, prog, labels))

        # 若有 progress 篩選，於記憶體過濾（進度是衍生值，無法直接 SQL where）
        if progress is not None:
            items = [it for it in items if it["progress"]["code"] == int(progress)]

        return {"total": total, "count": len(items), "limit": limit,
                "offset": offset, "items": items}

    def _task_row(self, r: dict, prog: st.Progress, labels: dict) -> dict:
        return {
            "id": r["id"],
            "task_sn": r["task_sn"],
            "name": r["name"],
            "status": r["status"],
            "status_label": st.label(st.TASK_STATUS, r["status"]),
            "pay_status": r["pay_status"],
            "pay_status_label": st.label(st.PAY_STATUS, r["pay_status"]),
            "task_amount": _num(r["task_amount"]),
            "estimated_total_amount": _num(r["estimated_total_amount"]),
            "total_amount": _num(r["total_amount"]),
            "start_at": r["start_at"], "end_at": r["end_at"],
            "recruit_count": r["recruit_count"],
            "recruited_count": r["recruited_count"],
            "recruit_deadline": r["recruit_deadline"],
            "payment_method_id": r["payment_method_id"],
            "payment_method_label": st.label(st.PAYMENT_METHOD, r["payment_method_id"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "published_at": r["published_at"],
            "city_id": r["city_id"], "district_id": r["district_id"],
            "publisher": self._labor_label(labels, r["created_by"]),
            "receiver": self._labor_label(labels, r.get("receiver_id")),
            "progress": prog.to_dict(),
        }

    # ── 任務詳情 ───────────────────────────────────────────────────────────
    def task_detail(self, task_sn: str) -> dict | None:
        t = self.db.query_one(
            "SELECT * FROM s_contract_tasks WHERE task_sn = %s", (task_sn,)
        )
        if not t:
            return None
        tid = t["id"]
        now = int(time.time())

        receiver_tasks = self.db.query_all(
            """SELECT id, receiver_id, status, task_status, agree_to_work, wage,
                      start_at, end_at, actual_start_at, actual_end_at,
                      canceled_at, rejected_at, failed_at, created_at, updated_at
               FROM s_contract_receiver_tasks WHERE task_id = %s ORDER BY id""",
            (tid,),
        )
        match_tasks = self.db.query_all(
            """SELECT id, receiver_id, type, status, `read`, displayed,
                      created_at, updated_at, publisher_declined_at
               FROM s_contract_receiver_match_tasks WHERE task_id = %s ORDER BY id""",
            (tid,),
        )
        change_logs = self.db.query_all(
            """SELECT id, publisher_id, receiver_id, status, more_info, created_at, created_by
               FROM s_contract_task_change_logs WHERE task_id = %s ORDER BY id""",
            (tid,),
        )

        # 進度用最新一筆接案者任務
        primary_rtt = receiver_tasks[-1]["task_status"] if receiver_tasks else None
        prog = st.derive_progress(
            task_status=t["status"], pay_status=t["pay_status"],
            recruit_deadline=t["recruit_deadline"],
            receiver_task_status=primary_rtt, now=now,
        )

        # 收集所有 labor id 一次解析
        ids = [t["created_by"]]
        ids += [r["receiver_id"] for r in receiver_tasks]
        ids += [r["receiver_id"] for r in match_tasks]
        ids += [r["receiver_id"] for r in change_logs]
        labels = self._labor_labels(ids)

        return {
            "task": self._task_row(
                {**t, "receiver_task_status": primary_rtt,
                 "receiver_id": receiver_tasks[-1]["receiver_id"] if receiver_tasks else None},
                prog, labels,
            ),
            "address": t.get("address"),
            "receiver_tasks": [
                {
                    "id": r["id"],
                    "receiver": self._labor_label(labels, r["receiver_id"]),
                    "status": r["status"],
                    "status_label": st.label(st.RECEIVER_TASK_STATUS, r["status"]),
                    "task_status": r["task_status"],
                    "task_status_label": st.label(st.RECEIVER_TASK_TASK_STATUS, r["task_status"]),
                    "agree_to_work": r["agree_to_work"],
                    "wage": _num(r["wage"]),
                    "start_at": r["start_at"], "end_at": r["end_at"],
                    "actual_start_at": r["actual_start_at"],
                    "actual_end_at": r["actual_end_at"],
                    "canceled_at": r["canceled_at"], "rejected_at": r["rejected_at"],
                    "failed_at": r["failed_at"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                }
                for r in receiver_tasks
            ],
            "match_tasks": [
                {
                    "id": r["id"],
                    "receiver": self._labor_label(labels, r["receiver_id"]),
                    "type": r["type"],
                    "status": r["status"],
                    "status_label": st.label(st.RECEIVER_MATCH_STATUS, r["status"]),
                    "read": r["read"], "displayed": r["displayed"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"],
                }
                for r in match_tasks
            ],
            "timeline": [
                {
                    "id": r["id"],
                    "status": r["status"],
                    "status_label": st.label(st.CHANGE_LOG_STATUS, r["status"]),
                    "actor": self._labor_label(labels, r["created_by"]),
                    "receiver": self._labor_label(labels, r["receiver_id"]),
                    "more_info": r["more_info"],
                    "created_at": r["created_at"],
                }
                for r in change_logs
            ],
        }

    # ══════════════════════════════════════════════════════════════════════
    # 工作系統（job）— 工作看板
    # ══════════════════════════════════════════════════════════════════════
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
        _apply_filters(where, params, filters, self.JOB_FILTERS)
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
            "hourly_wage": _num(r["hourly_wage"]),
            "estimated_total_amount": _num(r["estimated_total_amount"]),
            "total_amount": _num(r["total_amount"]),
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
                 "agree_to_work": r["agree_to_work"], "wage": _num(r["wage"]),
                 "total_wage": _num(r["total_wage"]),
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

    # ══════════════════════════════════════════════════════════════════════
    # 管理：打工夥伴 / 商家 / 店鋪（純清單，display_name 加密故以 phone/id 呈現）
    # ══════════════════════════════════════════════════════════════════════
    LABOR_FILTERS = {"status": ("status", "eq"), "valid_status": ("valid_status", "eq"),
                     "is_profile_complete": ("is_profile_complete", "eq")}

    def list_labors(self, *, q: str = "", filters: dict | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if q:
            where.append("(phone LIKE %s OR username LIKE %s OR CAST(id AS CHAR)=%s)")
            params += [f"%{q}%", f"%{q}%", q]
        _apply_filters(where, params, filters, self.LABOR_FILTERS)
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
        _apply_filters(where, params, filters, self.EMPLOYER_FILTERS)
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
                    "validation_type": ("validation_type", "eq")}

    def list_shops(self, *, q: str = "", filters: dict | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        where, params = [], []
        if q:
            where.append("(name LIKE %s OR branch_name LIKE %s OR CAST(id AS CHAR)=%s)")
            params += [f"%{q}%", f"%{q}%", q]
        _apply_filters(where, params, filters, self.SHOP_FILTERS)
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

    # ── 系統設置（唯讀）─────────────────────────────────────────────────────
    def settings_info(self) -> dict:
        s = self.settings
        return {
            "db_name": s.db_name, "db_host": s.db_host, "db_port": s.db_port,
            "api_base": s.api_base, "platform": s.platform,
            "deepseek_model": s.deepseek_model, "deepseek_base_url": s.deepseek_base_url,
            "deepseek_key_set": bool(s.deepseek_api_key),
            "counts": {
                "jobs": self.db.query_one("SELECT COUNT(*) c FROM s_jobs WHERE is_deleted=0")["c"],
                "contract_tasks": self.db.query_one("SELECT COUNT(*) c FROM s_contract_tasks WHERE is_deleted=0")["c"],
                "labors": self.db.query_one("SELECT COUNT(*) c FROM s_labors")["c"],
                "employers": self.db.query_one("SELECT COUNT(*) c FROM s_employers")["c"],
                "shops": self.db.query_one("SELECT COUNT(*) c FROM s_shops")["c"],
            },
        }

    # ── 統計（看板頂部）─────────────────────────────────────────────────────
    def stats(self) -> dict:
        rows = self.db.query_all(
            """SELECT t.status, t.pay_status, t.recruit_deadline,
                      prt.task_status AS receiver_task_status
               FROM s_contract_tasks t
               LEFT JOIN (
                   SELECT rt.task_id, rt.task_status
                   FROM s_contract_receiver_tasks rt
                   JOIN (SELECT task_id, MAX(id) mid
                         FROM s_contract_receiver_tasks GROUP BY task_id) m
                     ON rt.id = m.mid
               ) prt ON prt.task_id = t.id
               WHERE t.is_deleted = 0"""
        )
        now = int(time.time())
        by_progress: dict[int, int] = {}
        for r in rows:
            prog = st.derive_progress(
                task_status=r["status"], pay_status=r["pay_status"],
                recruit_deadline=r["recruit_deadline"],
                receiver_task_status=r["receiver_task_status"], now=now,
            )
            by_progress[prog.code] = by_progress.get(prog.code, 0) + 1

        active_codes = {st.P_MATCHING, st.P_HANDLE, st.P_WAITING_PAY,
                        st.P_WAITING_START, st.P_PROCESSING, st.P_WAITING_CONFIRM}
        return {
            "total": len(rows),
            "active": sum(v for k, v in by_progress.items() if k in active_codes),
            "completed": by_progress.get(st.P_TASK_COMPLETED, 0),
            "canceled": by_progress.get(st.P_CANCELED, 0)
                        + by_progress.get(st.P_TASK_FAILED, 0),
            "by_progress": [
                {"code": code, "title": st.PROGRESS_TITLE.get(code, str(code)),
                 "count": by_progress.get(code, 0)}
                for code in [st.P_MATCHING, st.P_HANDLE, st.P_WAITING_PAY,
                             st.P_WAITING_START, st.P_PROCESSING, st.P_WAITING_CONFIRM,
                             st.P_TASK_COMPLETED, st.P_REJECTED, st.P_TASK_FAILED,
                             st.P_CANCELED]
            ],
        }


def _num(v):
    """Decimal → float（JSON 可序列化）。"""
    if v is None:
        return None
    return float(v)
