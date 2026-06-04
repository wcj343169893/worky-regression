"""承攬制任務（contract）：任務看板清單 / 詳情 / 頂部統計。"""
from __future__ import annotations

import time

from .. import status as st
from .base import apply_filters, num


class ContractMixin:
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
        apply_filters(where, params, filters, self.TASK_FILTERS)
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
            "task_amount": num(r["task_amount"]),
            "estimated_total_amount": num(r["estimated_total_amount"]),
            "total_amount": num(r["total_amount"]),
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
                    "wage": num(r["wage"]),
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
