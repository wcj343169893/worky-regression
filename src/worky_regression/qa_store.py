"""QA 看板資料庫存取層（worky_qa_dashboard）。

把「用例（qa_cases）」與「每次執行結果（qa_runs / qa_run_steps）」落地到 MySQL，
取代原本散落的 results/*.json。

schema 由 SQLAlchemy 模型（qa_models）定義、Alembic 管理遷移；本類別只負責資料存取，
連線走 SQLAlchemy engine、查詢用顯式 SQL（沿用本專案 raw-SQL 風格）。讀取方法刻意回傳
「與 dashboard 前端既有結構一致」的形狀（只多帶 run_id），讓前端幾乎不用改。
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy import bindparam, text

from .config import Settings
from . import qa_models


def make_run_id(case_id: str, started_at: int) -> str:
    """每次執行唯一 id：用例 id + 秒級時間戳 + 6 hex，同秒多跑也不撞。"""
    return f"{case_id}-{started_at}-{secrets.token_hex(3)}"


class QAStore:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def _engine(self):
        return qa_models.get_engine(self.s)

    def migrate(self) -> None:
        """確保庫存在並把 schema 帶到最新（alembic upgrade head）。"""
        qa_models.migrate(self.s)

    # ── 寫入 ─────────────────────────────────────────────────────────────────
    def sync_cases(self, cases: list[dict[str, Any]]) -> None:
        """批次 upsert 用例註冊；同 id 多檔會記警告（後寫覆蓋，但保留警示）。"""
        rows = []
        seen: dict[str, str] = {}
        for c in cases:
            cid = c.get("id")
            if not cid:
                continue
            if cid in seen and seen[cid] != c.get("file"):
                print(f"[qa_store] 警告：用例 id 重複 '{cid}'（{seen[cid]} / {c.get('file')}）")
            seen[cid] = c.get("file", "")
            rows.append({
                "id": cid, "file": c.get("file", ""), "system": c.get("system", ""),
                "source": c.get("source", "builtin"), "description": c.get("description", ""),
                "step_count": int(c.get("step_count", 0)), "yaml": c.get("yaml", ""),
                "created_at": int(c.get("created_at", 0)),
                "parent_id": c.get("parent_id"),   # 頂層用例為 None
            })
        if not rows:
            return
        sql = text("""
            INSERT INTO qa_cases (id, file, `system`, source, description, step_count, yaml, created_at, parent_id)
            VALUES (:id, :file, :system, :source, :description, :step_count, :yaml, :created_at, :parent_id)
            ON DUPLICATE KEY UPDATE
              file=VALUES(file), `system`=VALUES(`system`), source=VALUES(source),
              description=VALUES(description), step_count=VALUES(step_count),
              yaml=VALUES(yaml), created_at=VALUES(created_at), parent_id=VALUES(parent_id)
        """)
        with self._engine.begin() as conn:
            conn.execute(sql, rows)

    def insert_run(self, *, run_id: str, case_id: str, system: str, status: str,
                   description: str, started_at: int, failed_at: int | None,
                   steps: list[dict[str, Any]], source: str = "run") -> None:
        """寫一次執行（qa_runs + qa_run_steps），同交易；同 run_id 重入會先清再插（冪等）。"""
        passed = sum(1 for s in steps if s.get("status") == "passed")
        total = len(steps)
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM qa_run_steps WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("DELETE FROM qa_runs WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("""
                INSERT INTO qa_runs
                  (run_id, case_id, `system`, status, description, started_at, passed, total, failed_at, source)
                VALUES (:run_id, :case_id, :system, :status, :description, :started_at, :passed, :total, :failed_at, :source)
            """), {
                "run_id": run_id, "case_id": case_id, "system": system, "status": status,
                "description": description, "started_at": int(started_at), "passed": passed,
                "total": total, "failed_at": failed_at, "source": source,
            })
            if steps:
                conn.execute(text("""
                    INSERT INTO qa_run_steps
                      (run_id, step_index, kind, name, status, elapsed_ms, error, observations)
                    VALUES (:run_id, :step_index, :kind, :name, :status, :elapsed_ms, :error, :observations)
                """), [{
                    "run_id": run_id, "step_index": int(s.get("index", i)), "kind": s.get("kind", ""),
                    "name": s.get("name", ""), "status": s.get("status", ""),
                    "elapsed_ms": int(s.get("elapsed_ms", 0)), "error": s.get("error"),
                    "observations": json.dumps(s.get("observations") or {}, ensure_ascii=False),
                } for i, s in enumerate(steps)])

    # ── 讀取（回傳前端既有形狀 + run_id）───────────────────────────────────────
    @staticmethod
    def _row(r) -> dict:
        return dict(r._mapping)

    def _latest_run(self, conn, case_id: str) -> dict | None:
        r = conn.execute(text(
            "SELECT * FROM qa_runs WHERE case_id=:c ORDER BY started_at DESC, run_id DESC LIMIT 1"
        ), {"c": case_id}).first()
        return self._row(r) if r else None

    def _transition_status(self, conn, run_id: str) -> list[str]:
        rows = conn.execute(text(
            "SELECT status FROM qa_run_steps WHERE run_id=:r AND kind='transition' ORDER BY step_index"
        ), {"r": run_id}).all()
        return [row[0] for row in rows]

    def run_count(self, case_id: str) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM qa_runs WHERE case_id=:c"), {"c": case_id}).scalar() or 0)

    def latest_summary(self, case_id: str) -> dict | None:
        """清單用：最近一次執行的彙總（含 transition_status 供 chip 著色）。"""
        with self._engine.connect() as conn:
            run = self._latest_run(conn, case_id)
            if not run:
                return None
            return {
                "run_id": run["run_id"], "status": run["status"], "started_at": run["started_at"],
                "passed": run["passed"], "total": run["total"], "failed_at": run["failed_at"],
                "transition_status": self._transition_status(conn, run["run_id"]),
            }

    def history(self, case_id: str, limit: int = 10) -> list[dict]:
        with self._engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT run_id, status, started_at, passed, total, failed_at
                FROM qa_runs WHERE case_id=:c ORDER BY started_at DESC, run_id DESC LIMIT :n
            """), {"c": case_id, "n": int(limit)}).all()
            return [self._row(r) for r in rows]

    def latest_full(self, case_id: str) -> dict | None:
        """詳情 / 步驟 modal 用：最近一次執行的完整步驟。"""
        with self._engine.connect() as conn:
            run = self._latest_run(conn, case_id)
            if not run:
                return None
            rows = conn.execute(text("""
                SELECT step_index, kind, name, status, elapsed_ms, error, observations
                FROM qa_run_steps WHERE run_id=:r ORDER BY step_index
            """), {"r": run["run_id"]}).all()
            steps = []
            for r in rows:
                m = r._mapping
                obs = m["observations"]
                if isinstance(obs, str):
                    try:
                        obs = json.loads(obs)
                    except Exception:  # noqa: BLE001
                        obs = {}
                steps.append({
                    "index": m["step_index"], "kind": m["kind"], "name": m["name"],
                    "status": m["status"], "elapsed_ms": m["elapsed_ms"],
                    "observations": obs or {}, "error": m["error"],
                })
            return {
                "run_id": run["run_id"], "status": run["status"],
                "started_at": run["started_at"], "failed_at": run["failed_at"], "steps": steps,
            }

    def case_seq(self, case_id: str) -> int | None:
        """用例的數字序號（並存於 slug id 之外，僅顯示用）。"""
        with self._engine.connect() as conn:
            v = conn.execute(text("SELECT seq FROM qa_cases WHERE id=:c"), {"c": case_id}).scalar()
            return int(v) if v is not None else None

    def case_id_exists(self, case_id: str) -> bool:
        with self._engine.connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM qa_cases WHERE id=:c LIMIT 1"), {"c": case_id}).first() is not None

    # ── 已執行實體（看板資料來源：只看本框架跑過的 SN）─────────────────────────
    # system → observations.saved 內的序號 key（白名單對映，不可拼使用者輸入進 SQL）。
    # 實測整庫 distinct saved keys 只有 task_sn / job_sn。
    _SN_KEY = {"contract": "task_sn", "job": "job_sn"}

    def executed_entities(self, system: str) -> list[dict]:
        """回傳本框架已執行過、且 observations.saved 內留有實體序號的清單。

        每個 SN 聚合成一筆：
          {sn, runs(該 SN 出現過的 run 數), last_run_id, last_status, last_started_at}
        last_* 取該 SN 最近一次 run（started_at desc, run_id desc）。
        其餘 system 一律回 []（白名單外不查）。
        """
        snkey = self._SN_KEY.get(system)
        if not snkey:
            return []
        # JSON 抽取：每個 (run, sn) 去重一次（同一 run 多步驟存同 SN 只算一次）。
        # snkey 來自白名單常量，非使用者輸入，可安全內嵌進 JSON path。
        sql = text(f"""
            SELECT DISTINCT r.run_id AS run_id, r.started_at AS started_at,
                   r.status AS status,
                   s.observations->>'$.saved.{snkey}' AS sn
            FROM qa_run_steps s
            JOIN qa_runs r ON r.run_id = s.run_id
            WHERE r.`system` = :sys
              AND s.observations->>'$.saved.{snkey}' IS NOT NULL
              AND s.observations->>'$.saved.{snkey}' <> ''
        """)
        with self._engine.connect() as conn:
            rows = [r._mapping for r in conn.execute(sql, {"sys": system}).all()]

        # Python 端聚合：每 SN 取 runs 計數 + 最近一筆（started_at desc, run_id desc）。
        agg: dict[str, dict] = {}
        for r in rows:
            sn = r["sn"]
            if sn is None or sn == "":
                continue
            sn = str(sn)
            cur = agg.get(sn)
            sa = int(r["started_at"] or 0)
            rid = r["run_id"]
            if cur is None:
                agg[sn] = {
                    "sn": sn, "runs": 1,
                    "last_run_id": rid, "last_status": r["status"], "last_started_at": sa,
                }
            else:
                cur["runs"] += 1
                # 比較「最近」：started_at desc，平手再比 run_id desc
                if (sa, str(rid)) > (cur["last_started_at"], str(cur["last_run_id"])):
                    cur["last_run_id"] = rid
                    cur["last_status"] = r["status"]
                    cur["last_started_at"] = sa
        return list(agg.values())

    def executed_sns(self, system: str) -> list[str]:
        """輕量版：只取已執行 SN 清單（給 service 組 IN 子句用）。"""
        return [e["sn"] for e in self.executed_entities(system)]

    # ── 子用例（主任務/子任務下鑽）─────────────────────────────────────────────
    def child_count(self, parent_id: str) -> int:
        """指定父用例的直接子用例數（parent_id 非保留字，不需反引號）。"""
        with self._engine.connect() as conn:
            return int(conn.execute(text(
                "SELECT COUNT(*) FROM qa_cases WHERE parent_id=:p"), {"p": parent_id}).scalar() or 0)

    def child_counts(self, ids: list[str]) -> dict[str, int]:
        """批次查多個父用例的子用例數（避免清單 N+1）；回傳 {parent_id: count}。"""
        ids = [i for i in ids if i]
        if not ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT parent_id, COUNT(*) AS n FROM qa_cases "
                "WHERE parent_id IN :ids GROUP BY parent_id"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
            return {r[0]: int(r[1]) for r in rows}
