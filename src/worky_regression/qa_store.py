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
import time
from typing import Any

import yaml

from sqlalchemy import bindparam, text

from .config import Settings
from . import qa_models


def _json_or(v, default):
    """欄位是字串就 json.loads，已是 dict/list 直接用，失敗回 default。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return default
    return v if v is not None else default


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

    def get_case_yaml(self, case_id: str) -> str | None:
        """讀回單一用例的 YAML（resume_worker 喚醒時重建 spec 用，免再掃檔）。"""
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT yaml FROM qa_cases WHERE id=:i"),
                               {"i": case_id}).first()
        return row.yaml if row else None

    def insert_run(self, *, run_id: str, case_id: str, system: str, status: str,
                   description: str, started_at: int, failed_at: int | None,
                   steps: list[dict[str, Any]], source: str = "run",
                   actors: dict | None = None) -> None:
        """寫一次執行（qa_runs + qa_run_steps），同交易；同 run_id 重入會先清再插（冪等）。

        actors：本次參與帳號快照（{role: {...}}），以 JSON 存 qa_runs.actors，供詳情頁展示。
        """
        passed = sum(1 for s in steps if s.get("status") == "passed")
        total = len(steps)
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM qa_run_steps WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("DELETE FROM qa_runs WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("""
                INSERT INTO qa_runs
                  (run_id, case_id, `system`, status, description, started_at, passed, total, failed_at, source, actors)
                VALUES (:run_id, :case_id, :system, :status, :description, :started_at, :passed, :total, :failed_at, :source, :actors)
            """), {
                "run_id": run_id, "case_id": case_id, "system": system, "status": status,
                "description": description, "started_at": int(started_at), "passed": passed,
                "total": total, "failed_at": failed_at, "source": source,
                "actors": json.dumps(actors or {}, ensure_ascii=False),
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

    # ── 逐步落庫（崩潰留痕）────────────────────────────────────────────────
    # begin_run / append_step 是執行期的即時記錄；正常跑完仍由 insert_run（冪等
    # 刪後重插）收尾，順帶修復任何漏寫的步驟。進程中途死掉時 run 會停在
    # status='running'，由看板啟動時 mark_dangling_runs() 收斂成 'interrupted'。

    def begin_run(self, *, run_id: str, case_id: str, system: str, description: str,
                  started_at: int, total: int, source: str = "run",
                  actors: dict | None = None) -> None:
        """執行開始即落一筆 status='running' 的 run（先清同 run_id 殘留，冪等）。

        actors 開頭就帶上：進程被 SIGTERM/kill 殺死時不會有收尾，這筆會被看板啟動
        收斂成 interrupted——沒有 actors 的話，配對史就看不見這次錄取了誰，
        被佔用的時段（30207）也推算不出來。
        """
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM qa_run_steps WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("DELETE FROM qa_runs WHERE run_id=:r"), {"r": run_id})
            conn.execute(text("""
                INSERT INTO qa_runs
                  (run_id, case_id, `system`, status, description, started_at, passed, total, failed_at, source, actors)
                VALUES (:run_id, :case_id, :system, 'running', :description, :started_at, 0, :total, NULL, :source, :actors)
            """), {
                "run_id": run_id, "case_id": case_id, "system": system,
                "description": description, "started_at": int(started_at),
                "total": int(total), "source": source,
                "actors": json.dumps(actors or {}, ensure_ascii=False),
            })

    def append_step(self, run_id: str, step: dict[str, Any]) -> None:
        """逐步落一筆步驟結果，同步刷新 run 列的 passed / failed_at。"""
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO qa_run_steps
                  (run_id, step_index, kind, name, status, elapsed_ms, error, observations)
                VALUES (:run_id, :step_index, :kind, :name, :status, :elapsed_ms, :error, :observations)
            """), {
                "run_id": run_id, "step_index": int(step.get("index", 0)),
                "kind": step.get("kind", ""), "name": step.get("name", ""),
                "status": step.get("status", ""), "elapsed_ms": int(step.get("elapsed_ms", 0)),
                "error": step.get("error"),
                "observations": json.dumps(step.get("observations") or {}, ensure_ascii=False),
            })
            if step.get("status") == "passed":
                conn.execute(text("UPDATE qa_runs SET passed=passed+1 WHERE run_id=:r"),
                             {"r": run_id})
            elif step.get("status") == "failed":
                conn.execute(text("UPDATE qa_runs SET failed_at=:i WHERE run_id=:r"),
                             {"i": int(step.get("index", 0)), "r": run_id})

    def mark_dangling_runs(self) -> int:
        """把殘留在 'running' 的 run 標成 'interrupted'，回傳筆數。

        run 跑在看板進程內的 thread，看板啟動時還停在 running 的必然是
        上次進程死掉留下的（CLI autotest 與看板同時跑的極端情況除外）。
        """
        with self._engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_runs SET status='interrupted' WHERE status='running'"))
            return int(res.rowcount or 0)

    # ── 長延時掛起/喚醒（Tier 2）──────────────────────────────────────────────
    # wait_until 發現距目標時間還很久時，recorder 把這次執行冷凍成 status='waiting'（落
    # checkpoint），由常駐 resume_worker 在 resume_at 到點時重建狀態續跑。

    def suspend_run(self, *, run_id: str, resume_at: int, resume_step_index: int,
                    checkpoint: dict[str, Any]) -> None:
        """把 run 冷凍成 waiting：記下何時醒（resume_at）、續跑哪一步、重建用的 checkpoint。

        不動 qa_run_steps——已跑步驟由 append_step 逐步落好；這裡只翻 status 與補欄位。
        """
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE qa_runs SET status='waiting', resume_at=:ra,
                  resume_step_index=:si, checkpoint=:cp WHERE run_id=:r
            """), {"ra": int(resume_at), "si": int(resume_step_index),
                   "cp": json.dumps(checkpoint, ensure_ascii=False), "r": run_id})

    def mark_run_running(self, run_id: str) -> None:
        """resume 開始實跑：把 resuming/waiting 翻回 running（續跑尾段，不重開 run 列）。"""
        with self._engine.begin() as conn:
            conn.execute(text("UPDATE qa_runs SET status='running' WHERE run_id=:r"),
                         {"r": run_id})

    def set_run_status(self, run_id: str, status: str) -> None:
        """直接設 run 狀態（resume_worker 遇不可恢復情形，如用例已刪，標 failed 收場）。"""
        with self._engine.begin() as conn:
            conn.execute(text("UPDATE qa_runs SET status=:s WHERE run_id=:r"),
                         {"s": status, "r": run_id})

    def load_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        """讀回某 run 已落地的步驟（供 resume seed steps）；鍵對齊 StepResult 欄位。"""
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT step_index, kind, name, status, elapsed_ms, error, observations "
                "FROM qa_run_steps WHERE run_id=:r ORDER BY step_index"), {"r": run_id}).all()
        out: list[dict[str, Any]] = []
        for r in rows:
            obs = r.observations
            obs = json.loads(obs) if isinstance(obs, str) else (obs or {})
            out.append({"index": int(r.step_index), "kind": r.kind, "name": r.name,
                        "status": r.status, "elapsed_ms": int(r.elapsed_ms),
                        "observations": obs, "error": r.error})
        return out

    def reset_resuming_runs(self) -> int:
        """啟動收斂：把卡在 'resuming'（worker 領取後、實跑前就掛了）的 run 退回 'waiting' 重試。"""
        with self._engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_runs SET status='waiting' WHERE status='resuming'"))
            return int(res.rowcount or 0)

    def claim_due_waiting_run(self, *, now: int | None = None) -> dict[str, Any] | None:
        """原子搶占一筆「到點該醒」的 waiting run（resume_at<=now），翻成 'resuming' 後回傳。

        以 UPDATE ... WHERE status='waiting' AND run_id=:r AND status 未變 搶占，多 worker
        併跑不會重領同筆。回傳含 checkpoint（已 parse）供重建；無到點者回 None。
        """
        now = int(now if now is not None else time.time())
        with self._engine.begin() as conn:
            row = conn.execute(text(
                "SELECT run_id FROM qa_runs WHERE status='waiting' AND resume_at IS NOT NULL "
                "AND resume_at<=:now ORDER BY resume_at LIMIT 1"), {"now": now}).first()
            if not row:
                return None
            res = conn.execute(text(
                "UPDATE qa_runs SET status='resuming' WHERE run_id=:r AND status='waiting'"),
                {"r": row.run_id})
            if not res.rowcount:
                return None   # 被別的 worker 搶先
            full = conn.execute(text(
                "SELECT run_id, case_id, `system`, resume_at, resume_step_index, checkpoint "
                "FROM qa_runs WHERE run_id=:r"), {"r": row.run_id}).first()
        cp = full.checkpoint
        cp = json.loads(cp) if isinstance(cp, str) else (cp or {})
        return {"run_id": full.run_id, "case_id": full.case_id, "system": full.system,
                "resume_at": int(full.resume_at or 0),
                "resume_step_index": int(full.resume_step_index or 0), "checkpoint": cp}

    def job_allocation_history(self, system: str, since: int) -> dict[str, Any]:
        """執行史 → 配號避撞（只讀看板庫，執行期不碰工作庫）。

        後端有一批「夥伴×商家×日」類限制（30229/30213 同企業/同商家每日僅限工作一次、
        30207 該時段已有確認工作、20009 商家單日發佈上限、dev 600s 發佈間隔），帳號池的
        單帳號 LRU 看不見「配對」維度，小池很快踩回燒過的組合。本方法從今天的
        qa_runs/qa_run_steps 還原三組事實供配發避讓：

        - accepted_pairs：{(employer_id, labor_id, work_date)} 今天有 J3 錄取成功的配對，
          work_date 為該工作的「工作日」(YYYY-MM-DD，無 vars 推不出時為 None)。被錄取的
          labor 從用例 YAML 的 J3 步驟 bind 解出（bind: {labor: laborN}，無 bind 即預設
          labor），對回 run.actors 取 user_id。30229/30213 是「同企業同工作日僅一次」，
          故配發端比對的是 work_date 而非發佈日——明天類用例不該被今天的近時段配對佔額度。
        - occupied：{labor_id: [(start_ts, end_ts), ...]}（30207 該時段已有確認工作——錄取
          即佔住表定時段，跨商家、run 死掉沒人打卡也一樣；配發端以「時段是否重疊」避讓，
          而非「有未過期佔用就避」。時段由用例 vars 的 start 偏移＋工時推算）。
        - publish：{employer_id: {count, last_at}} 今天 J1 發佈成功統計。
        """
        empty: dict[str, Any] = {"accepted_pairs": set(), "occupied": {}, "publish": {}}
        with self._engine.begin() as conn:
            runs = conn.execute(text(
                "SELECT run_id, case_id, started_at, actors FROM qa_runs "
                "WHERE `system`=:s AND started_at>=:t"), {"s": system, "t": int(since)}).all()
            if not runs:
                return empty
            steps = conn.execute(text(
                "SELECT run_id, name FROM qa_run_steps WHERE run_id IN :ids "
                "AND kind='transition' AND status='passed' "
                "AND (name LIKE 'J1%' OR name LIKE 'J3%')"
            ).bindparams(bindparam("ids", expanding=True)),
                {"ids": [r.run_id for r in runs]}).all()
            marks: dict[str, set[str]] = {}
            for s_ in steps:
                marks.setdefault(s_.run_id, set()).add(s_.name[:2])
            case_ids = sorted({r.case_id for r in runs if "J3" in (marks.get(r.run_id) or set())})
            specs = conn.execute(text(
                "SELECT id, yaml FROM qa_cases WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)),
                {"ids": case_ids}).all() if case_ids else []
        # 各用例 J3 步驟錄取的 actor 角色（bind: {labor: laborN}；無 bind 即 labor）
        # ＋時段參數（start 偏移 / 工時）——錄取即佔用該表定時段直到 end_at（30207），
        # 就算 run 中途死掉、沒人打卡，佔用仍然在。
        accept_roles: dict[str, set[str]] = {}
        slot_vars: dict[str, tuple[int, int]] = {}   # case_id -> (after_minutes, work_minutes)
        for sp in specs:
            try:
                spec = yaml.safe_load(sp.yaml) or {}
                roles = {((st.get("bind") or {}).get("labor") or "labor")
                         for st in (spec.get("path") or [])
                         if str(st.get("transition", "")).startswith("J3")}
                v = spec.get("vars") or {}
                if v.get("job_start_after_minutes") is not None:
                    slot_vars[sp.id] = (int(v["job_start_after_minutes"]),
                                       int(v.get("job_work_minutes", 120)))
            except Exception:  # noqa: BLE001 — 解析不了的用例保守當「錄取了所有 labor 角色」
                roles = {"*"}
            accept_roles[sp.id] = roles
        out = {"accepted_pairs": set(), "occupied": {}, "publish": {}}
        for r in runs:
            mk = marks.get(r.run_id) or set()
            a = r.actors
            a = json.loads(a) if isinstance(a, str) else (a or {})
            emp = str(((a.get("employer") or {}).get("user_id")) or "")
            if not emp:
                continue
            if "J1" in mk:
                p = out["publish"].setdefault(emp, {"count": 0, "last_at": 0})
                p["count"] += 1
                p["last_at"] = max(p["last_at"], int(r.started_at))
            if "J3" in mk:
                roles = accept_roles.get(r.case_id) or {"*"}
                # 工作時段 [start, end] 與工作日：發佈(≈started_at) + start 偏移、+ 工時。
                # 只有近時段用例（帶 job_start_after_minutes）能推算實際時段；其他用例工作
                # 排在 +3 天外、偏移未知 → 時段/工作日記為 None（配發端對 None 保守避讓）。
                # 避讓改為「同工作日同商家」(30229/30213) 與「時段重疊」(30207) 的精確判斷，
                # 而非「今天燒過就一律避」——否則明天類用例會把今天的近時段配對也誤算進額度。
                sv = slot_vars.get(r.case_id)
                if sv:
                    wstart = int(r.started_at) + sv[0] * 60
                    wend = int(r.started_at) + (sv[0] + sv[1]) * 60
                    wdate = time.strftime("%Y-%m-%d", time.localtime(wstart))
                else:
                    wstart = wend = 0
                    wdate = None
                for role, info in a.items():
                    if not (role.startswith("labor") and isinstance(info, dict)):
                        continue
                    if "*" not in roles and role not in roles:
                        continue
                    lid = str(info.get("user_id") or "")
                    if not lid:
                        continue
                    out["accepted_pairs"].add((emp, lid, wdate))
                    if wend:
                        out["occupied"].setdefault(lid, []).append((wstart, wend))
        return out

    def clear_runs(self, *, include_cases: bool = True) -> dict[str, int]:
        """清空所有執行類數據（「重新測試」用）：qa_run_steps + qa_runs；
        include_cases=True（預設）連 qa_cases 一併清，顯示序號（seq）歸零重來。

        刻意不動 qa_accounts（帳號池）/ qa_settings（後台帳密）/ qa_markups（頁面標記）。
        用例定義仍在 cases/*.yaml，下次載入看板會 sync_cases 自動重新註冊。
        用 TRUNCATE（順帶重置 AUTO_INCREMENT，序號從 1 起）；回傳各表清空前的列數。
        """
        # 子步驟 → 執行 → 用例 的順序清（無 FK 約束，順序僅為語意清楚）。
        tables = ["qa_run_steps", "qa_runs"]
        if include_cases:
            tables.append("qa_cases")
        counts: dict[str, int] = {}
        with self._engine.begin() as conn:
            for t in tables:
                counts[t] = int(conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() or 0)
                conn.execute(text(f"TRUNCATE TABLE {t}"))
        return counts

    # ── 看板可編輯設定（qa_settings key-value）─────────────────────────────────
    def get_settings(self, keys: list[str] | None = None) -> dict[str, str]:
        """讀設定；keys 為 None 時回全部。`key` 為 MySQL 保留字，加反引號。"""
        with self._engine.begin() as conn:
            if keys:
                stmt = text("SELECT `key`, value FROM qa_settings WHERE `key` IN :ks") \
                    .bindparams(bindparam("ks", expanding=True))
                rows = conn.execute(stmt, {"ks": keys}).fetchall()
            else:
                rows = conn.execute(text("SELECT `key`, value FROM qa_settings")).fetchall()
        return {r._mapping["key"]: r._mapping["value"] for r in rows}

    def set_settings(self, items: dict[str, str]) -> None:
        """upsert 設定（value 為 None 的鍵略過，不覆蓋既有）。"""
        rows = [{"k": k, "v": v} for k, v in items.items() if v is not None]
        if not rows:
            return
        sql = text("INSERT INTO qa_settings (`key`, value) VALUES (:k, :v) "
                   "ON DUPLICATE KEY UPDATE value = VALUES(value)")
        with self._engine.begin() as conn:
            conn.execute(sql, rows)

    # ── 頁面標記（qa_markups）─────────────────────────────────────────────────
    def insert_markup(self, *, route: str, selector: str | None, element_text: str | None,
                      rect: dict | None, content: str, screenshot_path: str | None,
                      created_at: int, kind: str = "page", ip: str | None = None) -> int:
        """新增一筆待處理標記，回傳自增 id。kind：page / feedback / global。"""
        sql = text("""
            INSERT INTO qa_markups
              (kind, route, selector, element_text, rect, content, screenshot_path, status, created_at, ip)
            VALUES (:kind, :route, :selector, :element_text, :rect, :content, :shot, 'pending', :ts, :ip)
        """)
        with self._engine.begin() as conn:
            res = conn.execute(sql, {
                "kind": kind, "route": route, "selector": selector, "element_text": element_text,
                "rect": json.dumps(rect, ensure_ascii=False) if rect is not None else None,
                "content": content, "shot": screenshot_path, "ts": created_at, "ip": ip})
            return int(res.lastrowid)

    @staticmethod
    def _markup_filters(status: str | None, q: str | None, route: str | None = None,
                        resolved: bool | None = None) -> tuple[str, dict]:
        """組標記查詢的 WHERE 子句 + 參數（status/route 精確、q 對 content/route/selector
        LIKE、resolved 布林——一般頁面畫框只拉「當前 route 的未解決標記」，不用全量）。"""
        clauses, params = [], {}
        if status:
            clauses.append("status=:st")
            params["st"] = status
        if q:
            clauses.append("(content LIKE :q OR route LIKE :q OR selector LIKE :q)")
            params["q"] = f"%{q}%"
        if route:
            clauses.append("route=:rt")
            params["rt"] = route
        if resolved is not None:
            clauses.append("resolved=:rv")
            params["rv"] = 1 if resolved else 0
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def list_markups(self, status: str | None = None, q: str | None = None,
                     limit: int = 100, offset: int = 0, route: str | None = None,
                     resolved: bool | None = None) -> list[dict]:
        """列標記（新到舊）；status/route/resolved 精確過濾、q 模糊搜尋；支援分頁 offset。"""
        where, params = self._markup_filters(status, q, route, resolved)
        sql = text(f"""
            SELECT id, kind, route, selector, element_text, rect, content, screenshot_path,
                   status, resolved, result, replies, created_at, updated_at,
                   ip, elapsed_ms, files_changed, commit_sha, rolled_back,
                   tokens_in, tokens_out, cost_usd
            FROM qa_markups {where}
            ORDER BY id DESC LIMIT :lim OFFSET :off
        """)
        params.update({"lim": int(limit), "off": int(offset)})
        with self._engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r._mapping)
            d["rect"] = _json_or(d.get("rect"), None)
            d["replies"] = _json_or(d.get("replies"), []) or []
            d["files_changed"] = _json_or(d.get("files_changed"), []) or []
            d["cost_usd"] = float(d.get("cost_usd") or 0)   # Decimal → float（給 JSON 序列化）
            out.append(d)
        return out

    def count_markups(self, status: str | None = None, q: str | None = None,
                      route: str | None = None, resolved: bool | None = None) -> int:
        """符合條件的標記總數（給分頁器算頁數）。"""
        where, params = self._markup_filters(status, q, route, resolved)
        with self._engine.connect() as conn:
            return int(conn.execute(
                text(f"SELECT COUNT(*) FROM qa_markups {where}"), params).scalar() or 0)

    def get_markup(self, markup_id: int) -> dict | None:
        rows = self.list_markups()
        for d in rows:
            if d["id"] == markup_id:
                return d
        with self._engine.connect() as conn:
            r = conn.execute(text("SELECT * FROM qa_markups WHERE id=:i"), {"i": markup_id}).first()
        if not r:
            return None
        d = dict(r._mapping)
        d["rect"] = _json_or(d.get("rect"), None)
        d["replies"] = _json_or(d.get("replies"), []) or []
        d["files_changed"] = _json_or(d.get("files_changed"), []) or []
        d["cost_usd"] = float(d.get("cost_usd") or 0)   # Decimal → float（給 JSON 序列化）
        return d

    def claim_pending_markup(self) -> dict | None:
        """原子領取最舊一筆 pending 標記，標記為 processing 後回傳；無則回 None。

        以 UPDATE ... WHERE status='pending' ORDER BY id LIMIT 1 搶占，避免多 worker 重領同筆。
        """
        with self._engine.begin() as conn:
            row = conn.execute(text(
                "SELECT id FROM qa_markups WHERE status='pending' ORDER BY id LIMIT 1"
            )).first()
            if not row:
                return None
            mid = int(row._mapping["id"])
            updated = conn.execute(text(
                "UPDATE qa_markups SET status='processing' WHERE id=:i AND status='pending'"
            ), {"i": mid}).rowcount
            if not updated:
                return None  # 被別的 worker 搶先
            r = conn.execute(text(
                "SELECT id, kind, route, selector, element_text, rect, content, screenshot_path, "
                "status, result, replies, created_at FROM qa_markups WHERE id=:i"), {"i": mid}).first()
        d = dict(r._mapping)
        d["rect"] = _json_or(d.get("rect"), None)
        d["replies"] = _json_or(d.get("replies"), []) or []
        return d

    def finish_markup(self, markup_id: int, *, status: str, result: str | None,
                      elapsed_ms: int = 0, files_changed: list | None = None,
                      tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0) -> None:
        """worker 處理完回寫狀態與摘要（status = done | failed）+ 耗時 + 動到的檔案
        + 本次 headless Claude 的 token 消耗與成本（再次優化會覆蓋為最近一次）。"""
        with self._engine.begin() as conn:
            conn.execute(text(
                "UPDATE qa_markups SET status=:s, result=:r, elapsed_ms=:ms, files_changed=:fc, "
                "tokens_in=:ti, tokens_out=:to, cost_usd=:cu WHERE id=:i"
            ), {"s": status, "r": result, "ms": int(elapsed_ms),
                "fc": json.dumps(files_changed or [], ensure_ascii=False),
                "ti": int(tokens_in), "to": int(tokens_out), "cu": float(cost_usd), "i": markup_id})

    def set_markup_commit(self, markup_id: int, sha: str) -> None:
        """記錄「已解決」時提交的 commit sha（回滾時 git revert 用）。"""
        with self._engine.begin() as conn:
            conn.execute(text(
                "UPDATE qa_markups SET commit_sha=:sha WHERE id=:i"), {"sha": sha, "i": markup_id})

    def set_markup_rolled_back(self, markup_id: int, note: str) -> None:
        """標記已回滾：rolled_back=1、resolved 清回 0，並把回滾說明附到 result 尾。"""
        with self._engine.begin() as conn:
            conn.execute(text(
                "UPDATE qa_markups SET rolled_back=1, resolved=0, "
                "result=CONCAT(COALESCE(result,''), :note) WHERE id=:i"
            ), {"note": f"\n\n── 回滾記錄 ──\n{note}", "i": markup_id})

    def reply_markup(self, markup_id: int, reply: str, *, at: int) -> bool:
        """追加一則使用者回覆並把 status 打回 pending，讓 worker 帶脈絡再次優化。

        回 False 表示找不到該標記。讀-改-寫在同一交易內，避免並發覆寫回覆串。
        """
        with self._engine.begin() as conn:
            row = conn.execute(text(
                "SELECT replies FROM qa_markups WHERE id=:i"), {"i": markup_id}).first()
            if not row:
                return False
            replies = _json_or(row._mapping["replies"], []) or []
            replies.append({"text": reply, "at": at})
            conn.execute(text(
                "UPDATE qa_markups SET replies=:rp, status='pending', result=result WHERE id=:i"
            ), {"rp": json.dumps(replies, ensure_ascii=False), "i": markup_id})
        return True

    def set_markup_resolved(self, markup_id: int, resolved: bool) -> bool:
        """設定「已解決」開關（1=已解決，源頁面不再畫框；0=取消解決，重新顯示）。

        純前端可視化的隱藏/顯示，不動 status，故 worker 處理流程不受影響。回 False 表示無此標記。
        """
        with self._engine.begin() as conn:
            res = conn.execute(text(
                "UPDATE qa_markups SET resolved=:v WHERE id=:i"),
                {"v": 1 if resolved else 0, "i": markup_id})
            return res.rowcount > 0

    def delete_markup(self, markup_id: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM qa_markups WHERE id=:i"), {"i": markup_id})

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
        """清單用：最近一次執行的彙總（含 transition_status 供 chip 著色、error 供失敗徽章懸停提示）。"""
        with self._engine.connect() as conn:
            run = self._latest_run(conn, case_id)
            if not run:
                return None
            error = conn.execute(text(
                "SELECT error FROM qa_run_steps WHERE run_id=:r AND status='failed' "
                "AND error IS NOT NULL ORDER BY step_index LIMIT 1"
            ), {"r": run["run_id"]}).scalar()
            out = {
                "run_id": run["run_id"], "status": run["status"], "started_at": run["started_at"],
                "passed": run["passed"], "total": run["total"], "failed_at": run["failed_at"],
                "transition_status": self._transition_status(conn, run["run_id"]),
                "error": error,
            }
            if run["status"] == "running":   # 執行中：附帶「正在跑哪一步」推算（刷新頁面後續顯倒數）
                out["live"] = self._live_progress(conn, run, case_id)
            return out

    def _live_progress(self, conn, run: dict, case_id: str) -> dict | None:
        """執行中 run 的當前步驟推算——頁面整刷後（SSE 閉包已不在）前端靠它還原
        「進行中閃爍 / 等待倒數」。

        逐步落庫只在步驟結束時插列，故「已落庫的最後一步 +1」即正在跑的步驟；
        步驟起點 ≈ started_at + Σelapsed_ms（執行串行，框架開銷可忽略）。
        """
        rows = conn.execute(text(
            "SELECT step_index, kind, elapsed_ms FROM qa_run_steps WHERE run_id=:r "
            "ORDER BY step_index"), {"r": run["run_id"]}).all()
        nxt = (max(r.step_index for r in rows) + 1) if rows else 0
        spec_yaml = conn.execute(text(
            "SELECT yaml FROM qa_cases WHERE id=:c"), {"c": case_id}).scalar()
        try:
            path = (yaml.safe_load(spec_yaml) or {}).get("path") or []
        except Exception:  # noqa: BLE001 — 用例 YAML 解析不了就不提供 live（只是顯示優化）
            return None
        if nxt >= len(path):
            return None
        cur = path[nxt]
        tdone = sum(1 for r in rows if r.kind == "transition")   # 下一個 transition 的 chip 序號
        elapsed = sum(int(r.elapsed_ms or 0) for r in rows) / 1000.0
        base = {"index": nxt, "step_started_at": int(run["started_at"] + elapsed)}
        if "wait_api" in cur:
            w = cur["wait_api"] or {}
            return {**base, "kind": "wait_api", "name": f"wait_api {w.get('query', '')}",
                    "wait_secs": float(w.get("timeout", 30)), "next_tindex": tdone}
        if "sleep" in cur:
            return {**base, "kind": "sleep", "name": f"sleep {cur['sleep']}s",
                    "wait_secs": float(cur["sleep"]), "next_tindex": tdone}
        if "transition" in cur:
            return {**base, "kind": "transition", "name": str(cur.get("transition", "?")),
                    "cur_tindex": tdone}
        return {**base, "kind": "other"}

    def run_counts(self, case_ids: list[str]) -> dict[str, int]:
        """批次取多支用例的執行次數（{id: count}），避免清單 N+1。"""
        ids = [i for i in case_ids if i]
        if not ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT case_id, COUNT(*) AS n FROM qa_runs "
                "WHERE case_id IN :ids GROUP BY case_id"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def latest_summaries(self, case_ids: list[str]) -> dict[str, dict]:
        """批次版 latest_summary（{case_id: summary}），把清單頁的 N+1 收斂成 ~3 條查詢。

        每支用例取最近一次 run（started_at desc, run_id desc），再一次性帶出所有
        run 的 transition 步驟狀態與首個失敗 error；執行中的 run 才逐一補 live 推算
        （執行中數量少，逐筆無妨）。輸出與 latest_summary 逐筆版完全一致。
        """
        ids = [i for i in case_ids if i]
        if not ids:
            return {}
        with self._engine.connect() as conn:
            runs = conn.execute(text("""
                SELECT t.* FROM (
                  SELECT q.*, ROW_NUMBER() OVER (
                           PARTITION BY case_id ORDER BY started_at DESC, run_id DESC) AS rn
                  FROM qa_runs q WHERE case_id IN :ids
                ) t WHERE t.rn = 1
            """).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).all()
            if not runs:
                return {}
            runs = [self._row(r) for r in runs]
            run_ids = [r["run_id"] for r in runs]
            # transition 步驟狀態：一次撈齊所有 run，Python 端按 run_id 分組（已按 step_index 排序）
            tss: dict[str, list[str]] = {}
            for rid, st in conn.execute(text(
                "SELECT run_id, status FROM qa_run_steps WHERE run_id IN :rids "
                "AND kind='transition' ORDER BY run_id, step_index"
            ).bindparams(bindparam("rids", expanding=True)), {"rids": run_ids}).all():
                tss.setdefault(rid, []).append(st)
            # 首個失敗步驟的 error（每 run 取 step_index 最小者，setdefault 只記第一筆）
            errs: dict[str, str] = {}
            for rid, err in conn.execute(text(
                "SELECT run_id, error FROM qa_run_steps WHERE run_id IN :rids "
                "AND status='failed' AND error IS NOT NULL ORDER BY run_id, step_index"
            ).bindparams(bindparam("rids", expanding=True)), {"rids": run_ids}).all():
                errs.setdefault(rid, err)
            out: dict[str, dict] = {}
            for run in runs:
                rid = run["run_id"]
                o = {
                    "run_id": rid, "status": run["status"], "started_at": run["started_at"],
                    "passed": run["passed"], "total": run["total"], "failed_at": run["failed_at"],
                    "transition_status": tss.get(rid, []),
                    "error": errs.get(rid),
                }
                if run["status"] == "running":
                    o["live"] = self._live_progress(conn, run, run["case_id"])
                elif run["status"] == "waiting":
                    o["wait"] = self._wait_info(conn, run)
                out[run["case_id"]] = o
        return out

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
            actors = run.get("actors")
            if isinstance(actors, str):
                try:
                    actors = json.loads(actors)
                except Exception:  # noqa: BLE001
                    actors = {}
            out = {
                "run_id": run["run_id"], "status": run["status"],
                "started_at": run["started_at"], "failed_at": run["failed_at"],
                "steps": steps, "actors": actors or {},
            }
            if run["status"] == "waiting":
                out["wait"] = self._wait_info(conn, run)
            return out

    def _wait_info(self, conn, run: dict[str, Any]) -> dict[str, Any]:
        """waiting run 的等待說明：等到何時 / 卡在哪一步 / 為什麼等這麼久。

        供看板對長延時掛起的 run 顯示倒數（至 resume_at）並在點開時解釋——這不是當機，
        是工作排在很久之後（如「明天 13:00」開工），跑完現在段後掛起、由 resume_worker
        到點喚醒續跑。step_label 由用例 YAML 的 `wait_until` 步驟（anchor/offset）推出。
        """
        idx = run.get("resume_step_index")
        info: dict[str, Any] = {
            "resume_at": int(run.get("resume_at") or 0),
            "resume_step_index": idx,
        }
        cp = run.get("checkpoint")
        cp = json.loads(cp) if isinstance(cp, str) else (cp or {})
        cvars = cp.get("vars") or {}
        for k in ("job_sn", "job_start_at", "job_end_at"):
            if cvars.get(k) is not None:
                info[k] = cvars[k]
        anchor = None
        step_label = None
        yml = conn.execute(text("SELECT yaml FROM qa_cases WHERE id=:i"),
                           {"i": run.get("case_id")}).scalar()
        if yml and idx is not None:
            try:
                path = (yaml.safe_load(yml) or {}).get("path") or []
                if 0 <= int(idx) < len(path) and "wait_until" in path[int(idx)]:
                    wu = path[int(idx)]["wait_until"] or {}
                    anchor = wu.get("anchor") or ("at" if "at" in wu else None)
                    off = int(wu.get("offset", 0))
                    kind = {"job_start_at": "工作表定開工",
                            "job_end_at": "工作表定結束"}.get(anchor, anchor or "指定時間")
                    rel = "" if off == 0 else (f"後 {off}s" if off > 0 else f"前 {-off}s")
                    step_label = f"等待「{kind}」{rel}".strip()
                # next_tindex = 喚醒後即將執行的下一顆 transition chip 序號（= 等待步驟之前
                # 的 transition 數）。前端據此讓該 chip 在掛起期間持續閃爍，免得看板像當機。
                if 0 <= int(idx) <= len(path):
                    info["next_tindex"] = sum(
                        1 for s in path[:int(idx)] if isinstance(s, dict) and "transition" in s)
            except Exception:  # noqa: BLE001 — 解析不了不致命，給通用文案
                pass
        info["anchor"] = anchor
        info["step_label"] = step_label or "等待長延時時間點"
        return info

    def case_seq(self, case_id: str) -> int | None:
        """用例的數字序號（並存於 slug id 之外，僅顯示用）。"""
        with self._engine.connect() as conn:
            v = conn.execute(text("SELECT seq FROM qa_cases WHERE id=:c"), {"c": case_id}).scalar()
            return int(v) if v is not None else None

    def case_seqs(self, case_ids: list[str]) -> dict[str, int]:
        """批次取多個用例的數字序號（{id: seq}），供清單一次查齊後按 seq 排序。"""
        if not case_ids:
            return {}
        sql = text("SELECT id, seq FROM qa_cases WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True))
        with self._engine.connect() as conn:
            rows = conn.execute(sql, {"ids": case_ids}).all()
        return {str(r[0]): int(r[1]) for r in rows if r[1] is not None}

    def case_id_by_seq(self, seq: int) -> str | None:
        """以看板顯示序號（#N，即 qa_cases.seq）反查用例 id；無此序號回 None。

        供「分解描述引用既有用例」（如「發佈一條 #2191 一樣的流程」）解析 #N 用。
        """
        with self._engine.connect() as conn:
            v = conn.execute(text(
                "SELECT id FROM qa_cases WHERE seq=:s"), {"s": seq}).scalar()
            return str(v) if v is not None else None

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
