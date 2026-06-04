"""承攬制任務（contract）：任務看板清單 / 詳情 / 頂部統計。"""
from __future__ import annotations

import time

from .. import status as st
from .base import num


class ContractMixin:
    # ── 任務清單 ───────────────────────────────────────────────────────────
    TASK_FILTERS = {"pay_status": ("t.pay_status", "eq"),
                    "payment_method_id": ("t.payment_method_id", "eq")}

    def list_tasks(self, *, q: str = "", progress: int | None = None,
                   publisher_id: int | None = None, filters: dict | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
        # Issue #1：全集 = 本框架已執行過、saved 留有 task_sn 的那批；不再全表掃描主倉。
        rows = self._executed_task_rows()

        # 文字 / 發案者 / 白名單篩選都在記憶體做（全集已縮小到已執行 SN）。
        if q:
            ql = q.lower()
            rows = [it for it in rows
                    if ql in (it["task_sn"] or "").lower()
                    or ql in (it["name"] or "").lower()]
        if publisher_id:
            pid = int(publisher_id)
            rows = [it for it in rows if (it.get("_created_by") or 0) == pid]
        rows = self._apply_mem_filters(rows, filters, self.TASK_FILTERS)

        # progress 篩選在分頁前套用（Issue #5：與 stats 同源、total/列數/分頁一致）。
        if progress is not None:
            rows = [it for it in rows if it["progress"]["code"] == int(progress)]

        total = len(rows)
        page = rows[int(offset):int(offset) + int(limit)]
        return {"total": total, "count": len(page), "limit": limit,
                "offset": offset, "items": page}

    # 記憶體版白名單篩選（對應 base.apply_filters 的 eq/min/max，但作用在已組好的列上）。
    # allow={param_key:(column, mode)}；列上欄位名取 column 去掉表別名（j.hourly_wage→hourly_wage）。
    @staticmethod
    def _apply_mem_filters(rows: list[dict], filters: dict | None, allow: dict) -> list[dict]:
        if not filters:
            return rows
        out = rows
        for key, (col, mode) in allow.items():
            v = filters.get(key)
            if v in (None, ""):
                continue
            field = col.split(".")[-1]
            if mode == "eq":
                out = [it for it in out if str(it.get(field)) == str(v)]
            elif mode == "min":
                out = [it for it in out if it.get(field) is not None and float(it[field]) >= float(v)]
            elif mode == "max":
                out = [it for it in out if it.get(field) is not None and float(it[field]) <= float(v)]
        return out

    def _executed_task_rows(self) -> list[dict]:
        """建出「已執行 task_sn 全集」對應的列（含主倉現況 + 降級列），供 list/stats 同源。

        排序：有主倉資料者依 task id desc；降級列排在最後（依 last_started_at desc）。
        每列都掛 last_run（last_run_id/last_status/last_started_at/runs）綁回測試框架。
        """
        executed = self.qa.executed_entities("contract")
        if not executed:
            return []
        ex_by_sn = {e["sn"]: e for e in executed}
        ex_sns = list(ex_by_sn.keys())
        now = int(time.time())

        # 縮限主倉查詢到已執行 SN（參數化 IN，不字串拼 SN）。
        ph = ",".join(["%s"] * len(ex_sns))
        db_rows = self.db.query_all(
            f"""
            SELECT t.id, t.task_sn, t.name, t.status, t.pay_status, t.task_amount,
                   t.estimated_total_amount, t.total_amount, t.start_at, t.end_at,
                   t.recruit_count, t.recruited_count, t.recruit_deadline,
                   t.payment_method_id, t.created_at, t.updated_at, t.published_at,
                   t.created_by, t.city_id, t.district_id,
                   prt.task_status AS receiver_task_status, prt.receiver_id
            FROM s_contract_tasks t
            LEFT JOIN (
                SELECT rt.task_id, rt.task_status, rt.status AS rstatus, rt.receiver_id
                FROM s_contract_receiver_tasks rt
                JOIN (SELECT task_id, MAX(id) mid
                      FROM s_contract_receiver_tasks GROUP BY task_id) m
                  ON rt.id = m.mid
            ) prt ON prt.task_id = t.id
            WHERE t.is_deleted = 0 AND t.task_sn IN ({ph})
            ORDER BY t.id DESC
            """,
            tuple(ex_sns),
        )

        labels = self._labor_labels([r["created_by"] for r in db_rows]
                                    + [r["receiver_id"] for r in db_rows])
        items: list[dict] = []
        found: set[str] = set()
        for r in db_rows:
            found.add(r["task_sn"])
            prog = st.derive_progress(
                task_status=r["status"], pay_status=r["pay_status"],
                recruit_deadline=r["recruit_deadline"],
                receiver_task_status=r["receiver_task_status"], now=now,
            )
            row = self._task_row(r, prog, labels)
            row["_created_by"] = r["created_by"]
            row["last_run"] = self._last_run(ex_by_sn.get(r["task_sn"]))
            items.append(row)

        # 降級列：已執行但主倉查無此 SN（被刪 / 分庫差異），不可整列消失。
        degraded = [e for sn, e in ex_by_sn.items() if sn not in found]
        degraded.sort(key=lambda e: e["last_started_at"], reverse=True)
        for e in degraded:
            items.append(self._degraded_task_row(e))
        return items

    @staticmethod
    def _last_run(entity: dict | None) -> dict | None:
        if not entity:
            return None
        return {"run_id": entity["last_run_id"], "status": entity["last_status"],
                "started_at": entity["last_started_at"], "runs": entity["runs"]}

    def _degraded_task_row(self, e: dict) -> dict:
        """主倉查無此 task_sn 時的降級列：以最後一次 run 狀態 / 時間呈現。"""
        prog = st.record_only_progress()
        return {
            "id": None, "task_sn": e["sn"], "name": "(僅記錄)",
            "status": None, "status_label": "-",
            "pay_status": None, "pay_status_label": "-",
            "task_amount": None, "estimated_total_amount": None, "total_amount": None,
            "start_at": None, "end_at": None,
            "recruit_count": None, "recruited_count": None, "recruit_deadline": None,
            "payment_method_id": None, "payment_method_label": "-",
            "created_at": None, "updated_at": e["last_started_at"], "published_at": None,
            "city_id": None, "district_id": None,
            "publisher": None, "receiver": None,
            "progress": prog.to_dict(),
            "_created_by": None,
            "last_run": self._last_run(e),
        }

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
        # Issue #1/#5：與 list_tasks 同源——同一批已執行列上算分布，total 必然一致。
        rows = self._executed_task_rows()
        by_progress: dict[int, int] = {}
        for it in rows:
            code = it["progress"]["code"]
            by_progress[code] = by_progress.get(code, 0) + 1

        active_codes = {st.P_MATCHING, st.P_HANDLE, st.P_WAITING_PAY,
                        st.P_WAITING_START, st.P_PROCESSING, st.P_WAITING_CONFIRM}
        # 分布段：線性 + 分支 + 降級（僅記錄）；只在實際出現的段顯示由前端過濾 count>0。
        codes = [st.P_MATCHING, st.P_HANDLE, st.P_WAITING_PAY,
                 st.P_WAITING_START, st.P_PROCESSING, st.P_WAITING_CONFIRM,
                 st.P_TASK_COMPLETED, st.P_REJECTED, st.P_TASK_FAILED,
                 st.P_CANCELED, st.P_RECORD_ONLY]
        return {
            "total": len(rows),
            "active": sum(v for k, v in by_progress.items() if k in active_codes),
            "completed": by_progress.get(st.P_TASK_COMPLETED, 0),
            "canceled": by_progress.get(st.P_CANCELED, 0)
                        + by_progress.get(st.P_TASK_FAILED, 0),
            "by_progress": [
                {"code": code, "title": st.PROGRESS_TITLE.get(code, str(code)),
                 "count": by_progress.get(code, 0)}
                for code in codes
            ],
        }
