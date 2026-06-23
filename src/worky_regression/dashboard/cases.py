"""測試用例層：列出 cases/*.yaml，執行結果落地 / 讀取 worky_qa_dashboard，並承接「執行」與「AI 分解」。

純檢視 + 觸發：
  - list/detail 讀 YAML（用例定義）+ qa_store（執行結果）。
  - run_case / decompose 會真的登入 + 呼叫被測 API（同步），複用既有 autotest/planner 管線，
    執行結果寫入 worky_qa_dashboard。
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

from ..config import Settings
from ..qa_store import QAStore

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "cases"
GENERATED_DIR = CASES_DIR / "generated"


def _detect_system(spec: dict) -> str:
    """以第一個 transition 前綴判系統：J* → job，T* → contract，A* → activity。

    真機軌（B）用例不走 transition，而是 kind=maestro / 帶 device 區塊的 Maestro flow，
    一律歸 "app"（DeviceRunner 執行；不連被測 DB、不配帳號池）。
    """
    if spec.get("kind") == "maestro" or spec.get("device"):
        return "app"
    for st in spec.get("path", []):
        t = str(st.get("transition", ""))
        if t[:1] == "J":
            return "job"
        if t[:1] == "T":
            return "contract"
        if t[:1] == "A":
            return "activity"
    return "contract"


def _transitions(spec: dict) -> list[str]:
    return [st["transition"] for st in spec.get("path", []) if st.get("transition")]


def _case_files() -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = [(p, "builtin") for p in sorted(CASES_DIR.glob("*.yaml"))]
    if GENERATED_DIR.is_dir():
        files += [(p, "generated") for p in sorted(GENERATED_DIR.glob("*.yaml"))]
    return files


# YAML 讀取/解析快取：依 (path, mtime) 命中，避免清單每次重讀重解析全部用例檔
# （90 檔約 150ms），並讓 _case_record 不必為了 yaml 欄位二次讀檔。檔案一改 mtime
# 變動即失效重載，不會讀到舊內容。
_yaml_cache: dict[str, tuple[float, str, dict | None]] = {}


def _read_yaml(path: Path) -> tuple[float, str, dict | None]:
    """回傳 (mtime, 原始文字, spec)；spec 為 None 表壞檔或非用例（無 path 欄）。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    hit = _yaml_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw) or {}
        spec = parsed if isinstance(parsed, dict) and "path" in parsed else None
    except Exception:  # noqa: BLE001 — 壞檔略過
        raw, spec = "", None
    entry = (mtime, raw, spec)
    _yaml_cache[str(path)] = entry
    return entry


def _load_yaml(path: Path) -> dict | None:
    return _read_yaml(path)[2]


def _case_id(spec: dict, path: Path) -> str:
    """用例 id：有 id 用 id，否則用檔名 stem（永不 unnamed，保證可追溯）。"""
    return str(spec.get("id") or path.stem)


def _case_record(path: Path, source: str, spec: dict) -> dict:
    """組 qa_cases upsert / 清單共用的用例基本資料。"""
    # mtime 與原始文字共用 _read_yaml 快取，免為了 created_at / yaml 欄位再讀檔兩次
    mtime, raw, _ = _read_yaml(path)
    # 父用例 id 來自 spec 的 parent 欄；無則頂層（None）
    parent = spec.get("parent")
    return {
        "id": _case_id(spec, path),
        "file": path.name,
        "system": _detect_system(spec),
        "source": source,
        "description": str(spec.get("description", "")).strip(),
        "step_count": len(spec.get("path", [])),
        "yaml": raw,
        "created_at": int(mtime),
        "parent_id": str(parent) if parent else None,
    }


class CaseStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.qa = QAStore(self.settings)

    # ── 清單 ─────────────────────────────────────────────────────────────────
    def list_cases(self, system: str | None = None, q: str = "",
                   limit: int = 20, offset: int = 0,
                   parent_id: str = "__root__") -> dict:
        """列用例。

        parent_id：
          - "__root__"（預設）→ 只回頂層用例（parent_id 為空者）。
          - 具體 id → 只回該父用例的直接子用例。
        每筆 item 附 parent_id 與 child_count，供前端決定是否顯示「子任務」按鈕。
        """
        ql = q.lower().strip()
        records: list[tuple[dict, dict]] = []   # (case_record, spec)
        child_counts: dict[str, int] = {}        # parent_id → 直接子用例數（依 YAML 全集計）
        for path, source in _case_files():
            spec = _load_yaml(path)
            if spec is None:
                continue
            rec = _case_record(path, source, spec)
            # 子用例數以 YAML 全集為準（不受 system/q/parent 過濾影響），避免「子用例尚未
            # 入庫 → child_count 為 0 → 按鈕不出現 → 無法下鑽 → 永不入庫」的死結
            if rec["parent_id"]:
                child_counts[rec["parent_id"]] = child_counts.get(rec["parent_id"], 0) + 1
            if system and rec["system"] != system:
                continue
            if ql and ql not in (rec["id"] + " " + rec["description"]).lower():
                continue
            # parent 過濾在記憶體做（與 system/q 同層）：頂層只看 parent 為空者，否則看指定父
            if parent_id == "__root__":
                if rec["parent_id"]:
                    continue
            elif rec["parent_id"] != parent_id:
                continue
            records.append((rec, spec))
        # 用例註冊：把（過濾後）用例 upsert 進 qa_cases，保證每筆用例都有 id
        self.qa.sync_cases([rec for rec, _ in records])
        # 排序鍵：sync 後一次查齊所有 seq（看板顯示的 # 編號），供下方按 id（seq）倒序
        seqs = self.qa.case_seqs([rec["id"] for rec, _ in records])

        items = [{
            "id": rec["id"],
            "file": rec["file"],
            "description": rec["description"],
            "system": rec["system"],
            "source": rec["source"],
            "created_at": rec["created_at"],
            "step_count": rec["step_count"],
            "parent_id": rec["parent_id"],
            "transitions": _transitions(spec),
            # 列表層帶上 skip 旗標與原因，讓「略過」徽章不必下鑽就能看到為什麼被略過
            "skip": bool(spec.get("skip")),
            "skip_reason": str(spec.get("skip_reason", "")).strip(),
        } for rec, spec in records]
        # 依 id（看板顯示的 # 序號，即 qa_cases.seq）倒序——新建用例排最前；
        # 個別無 seq 者（理論上 sync 後不會發生）以建立時間倒序墊底。
        for it in items:
            it["seq"] = seqs.get(it["id"])
        items.sort(key=lambda x: (x["seq"] is not None,
                                  x["seq"] if x["seq"] is not None else x["created_at"]),
                   reverse=True)
        total = len(items)
        page = items[offset:offset + limit] if limit else items
        # 每頁項目的執行彙總從 DB 取（只查當頁）。批次版收斂 N+1：整頁一次撈齊
        # 最近 run 彙總與執行次數，避免每列各開連線、各打數條查詢（清單變慢的主因）。
        page_ids = [it["id"] for it in page]
        summaries = self.qa.latest_summaries(page_ids)
        run_counts = self.qa.run_counts(page_ids)
        for it in page:
            it["last_result"] = summaries.get(it["id"])
            it["run_count"] = run_counts.get(it["id"], 0)
            it["child_count"] = child_counts.get(it["id"], 0)
        return {"items": page, "total": total}

    # ── 詳情 ─────────────────────────────────────────────────────────────────
    def _find(self, case_id: str) -> tuple[Path, str, dict] | None:
        for path, source in _case_files():
            spec = _load_yaml(path)
            if spec is not None and _case_id(spec, path) == case_id:
                return path, source, spec
        return None

    def case_detail(self, case_id: str) -> dict | None:
        found = self._find(case_id)
        if found is None and case_id.isdigit():
            # 純數字視為看板序號（#N）回退反查：分解輸入框點擊 #N 引用查看用例走這裡
            cid = self.qa.case_id_by_seq(int(case_id))
            found = self._find(cid) if cid else None
        if found is None:
            return None
        path, source, spec = found
        case_id = _case_id(spec, path)   # seq 回退時換回真正 id（history / 前端後續操作要用）
        steps = []
        for i, st in enumerate(spec.get("path", [])):
            if "db_exec" in st:
                steps.append({"index": i, "kind": "db_exec", "name": "db_exec",
                              "sql": st["db_exec"], "flush_cache": st.get("flush_cache", False)})
            elif "maestro" in st:
                m = st["maestro"] or {}
                steps.append({"index": i, "kind": "maestro",
                              "name": m.get("name", f"step{i}"), "flow": m.get("flow", "")})
            else:
                steps.append({"index": i, "kind": "transition", "name": st.get("transition", "?"),
                              "save": st.get("save"), "expect": st.get("expect")})
        return {
            "id": case_id, "file": path.name, "source": source,
            "system": _detect_system(spec),
            "description": str(spec.get("description", "")).strip(),
            "skip": bool(spec.get("skip")),
            "skip_reason": str(spec.get("skip_reason", "")).strip(),
            "steps": steps,
            "yaml": path.read_text(encoding="utf-8"),
            "history": self.qa.history(case_id, limit=10),
            "last_result": self.qa.latest_full(case_id),
        }

    def case_steps(self, case_id: str) -> dict | None:
        """chip 對齊的每步詳情：endpoints.yaml 規格 + 最近一次執行結果，供前端 modal 顯示。"""
        from ..registry import unit_spec

        found = self._find(case_id)
        if found is None:
            return None
        path, source, spec = found
        last = self.qa.latest_full(case_id)
        # 最近結果中的 transition 步驟，依序對齊 path 內的 transition 步驟
        all_steps = (last or {}).get("steps", [])
        res_trans = [s for s in all_steps if s.get("kind") == "transition"]
        # 每步執行時刻：DB 只存 run 起始時間 + 每步耗時，這裡用「run 起始 + 前面所有步驟
        # （含 sleep / db_exec）累計耗時」推算各步開始時刻（忽略步間極小開銷，秒級足夠）。
        run_t0 = (last or {}).get("started_at")
        step_t0: dict[int, float | None] = {}
        acc = 0
        for s in all_steps:
            step_t0[s["index"]] = (run_t0 + acc / 1000.0) if run_t0 else None
            acc += int(s.get("elapsed_ms") or 0)
        steps = []
        ti = 0
        for st in spec.get("path", []):
            name = st.get("transition")
            if not name:
                continue
            try:
                us = unit_spec(name)
            except Exception:  # noqa: BLE001 — 找不到單元規格時只回名稱
                us = {}
            rs = res_trans[ti] if ti < len(res_trans) else None
            steps.append({
                "index": ti,
                "name": name,
                "short": name.split("_")[0],
                "actor": us.get("actor"),
                "method": us.get("method"),
                "endpoint": us.get("endpoint"),
                "doc_id": us.get("doc_id"),
                "summary": us.get("summary"),
                "request": us.get("request"),
                "expect": st.get("expect") or {},
                "side_effects": us.get("side_effects"),
                "push": us.get("push"),
                "result": {
                    "status": rs.get("status"),
                    "elapsed_ms": rs.get("elapsed_ms"),
                    # 略過的步驟沒真的執行，不給推算時刻（前端就不顯示「執行於」）
                    "started_at": step_t0.get(rs["index"]) if rs.get("status") != "skipped" else None,
                    "error": rs.get("error"),
                    "observations": rs.get("observations"),
                } if rs else None,
            })
            ti += 1
        return {
            "id": case_id, "system": _detect_system(spec),
            "run_id": (last or {}).get("run_id"),
            "started_at": (last or {}).get("started_at"),
            "steps": steps,
        }

    # ── 執行 / 分解（會真的打被測 API）────────────────────────────────────────
    def _run_spec(self, spec: dict, *, source: str = "builtin", file: str = "",
                  actors: dict | None = None, on_event=None,
                  device_lock_wait: float = 0.0) -> dict:
        from uuid import uuid4

        from ..autotest import _actors_for, actor_swapper_for, required_actors
        from ..qa_accounts import AccountPool
        from ..recorder import RecordingRunner
        from ..verifier import DBVerifier

        system = _detect_system(spec)
        # 執行前先確保該用例已註冊進 qa_cases（run 的 case_id 才有對應用例）
        self.qa.sync_cases([{
            "id": spec.get("id", ""), "file": file, "system": system, "source": source,
            "description": str(spec.get("description", "")).strip(),
            "step_count": len(spec.get("path", [])),
            "yaml": yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
            "created_at": 0,
        }])
        # skip 用例（時間鎖 / 無 API 可替代）：不配帳號、不連 DB、不打被測 API，
        # 只記一筆全 skipped 的 run（避免為跑不了的用例白白佔用帳號池或觸發 PoolShortage）。
        if spec.get("skip"):
            return RecordingRunner(None, qa_store=self.qa, system=system).run(
                spec, actors={}, on_event=on_event).to_dict()
        # 真機軌（B）：UI 層執行，不連被測 DB、不配帳號池；走 DeviceRunner（maestro CLI），
        # 但落庫與 SSE 事件協定與 API 軌一致，看板沿用。
        if system == "app":
            from ..device_runner import DeviceRunner
            # device_lock_wait：看板 inline 執行預設 0（裝置忙就快速失敗）；device_worker
            # 背景排隊時傳大值，等到輪到它再跑（單裝置序列化）。
            return DeviceRunner(self.settings, qa_store=self.qa, system=system,
                                lock_wait_sec=device_lock_wait).run(
                spec, on_event=on_event).to_dict()
        db = DBVerifier(self.settings.for_system(system))   # contract/job 分庫
        # on_event（可選）：透傳給 RecordingRunner 做逐步事件回呼（看板 SSE 即時刷新用）
        # actor_swapper：步驟撞 30229/30213（夥伴×商家×日配對燒掉）時自動從池換夥伴重試
        runner = RecordingRunner(db, qa_store=self.qa, system=system,
                                 actor_swapper=actor_swapper_for(system, self.settings))
        # actors 可由呼叫端覆蓋（換號時帶入「已排除原帳號」的 actors）：沿用舊行為，不上租約。
        if actors is not None:
            return runner.run(spec, actors=actors, on_event=on_event).to_dict()
        # 正常配發：每次 run 用唯一 owner 配發＋整組上租約（並行批量執行的帳號互斥；
        # 池內已租帳號任何 acquire 都不再配出），結束（含拋錯）一律歸還。
        # required：用例引用的具名 actor（labor3 / labor_lacking_*）升級為硬需求，
        # 配不到在執行前就 PoolShortage，不會跑到中途 KeyError 白燒發佈。
        lease_owner = f"run-{spec.get('id') or 'case'}-{uuid4().hex[:8]}"
        actors = _actors_for(system, self.settings, required=required_actors(spec),
                             case_vars=spec.get("vars"), lease_owner=lease_owner)
        try:
            return runner.run(spec, actors=actors, on_event=on_event).to_dict()
        finally:
            # release 作用域須與上租約一致（contract 帳號掛 contract 庫的 db_name 下）
            AccountPool(self.settings.for_system(system)).release(lease_owner)

    def run_case(self, case_id: str, *, device_lock_wait: float = 0.0) -> dict:
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        path, source, spec = found
        return self._run_spec(spec, source=source, file=path.name,
                              device_lock_wait=device_lock_wait)

    def clear_all(self, include_cases: bool = True) -> dict:
        """清空所有測試用例執行數據（「重新測試」用）；不動帳號池 / 後台設定 / 頁面標記。

        用例定義仍在 cases/*.yaml；清掉 qa_cases 後下次列出清單會自動重新註冊（seq 歸零）。
        """
        counts = self.qa.clear_runs(include_cases=include_cases)
        return {"ok": True, "cleared": counts, "total": sum(counts.values())}

    def run_case_streaming(self, case_id: str, on_event) -> dict:
        """同 run_case，但逐步把執行事件回呼給 on_event（看板 SSE 用）。

        執行模型與 run_case 完全相同（同步、同樣落地 worky_qa_dashboard），差別只在
        多了 on_event 回呼。回傳值同 run_case（完整 RunResult dict），供呼叫端收尾。
        """
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        path, source, spec = found
        return self._run_spec(spec, source=source, file=path.name, on_event=on_event)

    # ── 失敗步驟的 AI 分析 / 換號重跑（step modal「分析 / 重試 / 換一個號」）──────
    def _failed_step(self, case_id: str, step_index: int) -> tuple[dict, dict]:
        """回傳 (該步詳情, 整支 steps 資料)；step_index 越界則拋錯。"""
        data = self.case_steps(case_id)
        if data is None:
            raise ValueError(f"找不到用例 {case_id}")
        steps = data.get("steps") or []
        if not (0 <= step_index < len(steps)):
            raise ValueError(f"步驟序號 {step_index} 超出範圍（共 {len(steps)} 步）")
        return steps[step_index], data

    def analyze_failure(self, case_id: str, step_index: int) -> dict:
        """把失敗步驟的情境餵給 AI，回傳診斷 + 建議（不自動採取行動）。"""
        from ..planner import analyze_failure as _analyze

        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        _, _, spec = found
        step, data = self._failed_step(case_id, step_index)
        res = step.get("result") or {}
        context = {
            "case_id": case_id,
            "system": data.get("system"),
            "case_description": str(spec.get("description", "")).strip(),
            "step_index": step_index,
            "transition": step.get("name"),
            "actor": step.get("actor"),
            "method": step.get("method"),
            "endpoint": step.get("endpoint"),
            "doc_id": step.get("doc_id"),
            "expect": step.get("expect"),
            "status": res.get("status"),
            "error": res.get("error"),
            "observations": res.get("observations"),
        }
        out = _analyze(context, self.settings)
        out["step_index"] = step_index
        out["transition"] = step.get("name")
        return out

    @staticmethod
    def _pool_role(actor_name: str) -> str | None:
        """把 actor 名對映到帳號池 role；非池角色（publisher/receiver 等）回 None。"""
        a = (actor_name or "").lower()
        if a.startswith("labor"):
            return "labor"
        if a.startswith("employer"):
            return "employer"
        return None

    def swap_account(self, case_id: str, step_index: int) -> dict:
        """排除失敗步驟 actor 目前用的帳號，配池中另一個同能力號，整支重跑。"""
        from ..autotest import _actors_for, required_actors
        from ..qa_accounts import PoolShortage

        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        path, source, spec = found
        system = _detect_system(spec)
        step, _ = self._failed_step(case_id, step_index)
        actor_name = step.get("actor") or ""
        role = self._pool_role(actor_name)
        if system not in ("job", "activity") or role is None:
            raise ValueError(
                f"步驟 actor「{actor_name or '未知'}」非帳號池角色（{system} 系統），不支援換號；"
                "contract 的 publisher/receiver 為固定 audit 帳號，請改用『重試』或回報主倉。")
        # 先以正常配發讀出「目前會用到」的帳號（acquire 對 role 的首選），再排除它重配
        req = required_actors(spec)
        before = _actors_for(system, self.settings, required=req, case_vars=spec.get("vars"))
        cur = before.get(actor_name) or before.get(role)
        cur_id = getattr(cur, "user_id", None)
        if cur_id is None:
            raise ValueError(f"無法判定 actor「{actor_name}」目前使用的帳號")
        try:
            swapped = _actors_for(system, self.settings, exclude={role: [str(cur_id)]},
                                  required=req, case_vars=spec.get("vars"))
        except PoolShortage as e:
            # 排除目前帳號後湊不齊本用例所需的合格帳號 —— 通常是合格號已全用於本用例、池無多餘替補。
            # 不把原始候選清單直接丟給使用者，改回可行動的指引。
            raise ValueError(
                f"帳號池沒有可替換的 {role}：排除目前帳號 {cur_id} 後，剩餘符合能力的帳號不足本用例所需。\n"
                f"通常是合格帳號已全部用於本用例、池中無多餘替補。建議：\n"
                f"① 若疑似時序/快取污染 → 改用『重試』；\n"
                f"② 用 provision() 補一個合格 {role} 帳號到池後再換號；\n"
                f"③ 若疑似被測對象 bug → 回報主倉。\n"
                f"（池配發明細：{e}）"
            ) from e
        new = swapped.get(actor_name) or swapped.get(role)
        new_id = getattr(new, "user_id", None)
        result = self._run_spec(spec, source=source, file=path.name, actors=swapped)
        return {
            "result": result,
            "swapped": {"actor": actor_name, "role": role,
                        "from": str(cur_id), "to": str(new_id) if new_id is not None else None},
        }

    def _unique_case_id(self, base: str) -> str:
        """AI 產生用例時防撞號：已存在（qa_cases 或 generated/ 檔）就加 -2/-3…後綴。"""
        def taken(cid: str) -> bool:
            return (GENERATED_DIR / f"{cid}.yaml").exists() or self.qa.case_id_exists(cid)
        if not taken(base):
            return base
        n = 2
        while taken(f"{base}-{n}"):
            n += 1
        return f"{base}-{n}"

    def copy_case(self, case_id: str) -> dict:
        """以既有用例的 spec 為範本快速再建一條新用例。

        只複製 spec（YAML 內容），**不複製執行歷史**：
        執行歷史是 qa_runs，本來就以 case_id 區分；新用例拿到全新的 id（天然無歷史），
        全程不碰 qa_runs，故新用例從零開始。
        以來源 id 為基底取防撞號（`<源id>-copy`），深拷貝來源 spec 再改 id，
        避免改到 _find 回傳的來源 spec 物件；落地直接複用 decompose_commit（防撞 + 寫檔 + sync_cases）。
        """
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        _, source, spec = found
        spec_copy = copy.deepcopy(spec)              # 深拷貝，絕不動到來源 spec 物件
        spec_copy["id"] = self._unique_case_id(f"{case_id}-copy")
        out = self.decompose_commit(spec_copy, run=False)   # 落地（再防撞 + 寫檔 + 不執行）
        return {"id": out["spec"]["id"], "saved": out["saved"],
                "system": out["system"], "source": source}

    def republish_case(self, case_id: str) -> dict:
        """以既有用例 spec 為範本「重新發佈」——複製成全新 id 後立即執行。

        針對「發佈資訊與時間高度綁定」的用例（例：『1 小時後開始』在不同時間發佈，
        工作起始時間就不同）：每次重新發佈都是一筆**全新獨立記錄**，不沿用、不牽連歷史。

        為何不牽連原用例與歷史：
          - 深拷貝來源 spec（`copy.deepcopy`），絕不動到 _find 回傳的來源 spec 物件；
          - 新 id 用 `_unique_case_id(f"{case_id}-pub")`（與複製的 `-copy` 後綴區隔，
            語意是「再發佈一次」），天然防撞；
          - 執行歷史是 qa_runs，本來就以 case_id 區分，新 id 沒有任何歷史，
            執行結果只會掛在這條新記錄下，完全不碰原用例的 qa_runs。
        落地直接複用 decompose_commit（再防撞 + 寫檔 + sync_cases），run=True 立即執行。
        回傳含 result（形狀同 run_case 回傳），供前端顯示這次發佈的執行結果。
        """
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        _, source, spec = found
        spec_copy = copy.deepcopy(spec)              # 深拷貝，絕不動到來源 spec 物件
        spec_copy["id"] = self._unique_case_id(f"{case_id}-pub")
        out = self.decompose_commit(spec_copy, run=True)    # 落地（再防撞 + 寫檔 + 立即執行）
        return {"id": out["spec"]["id"], "saved": out["saved"],
                "system": out["system"], "source": source, "result": out["result"]}

    def suggest_tab(self, description: str) -> dict:
        """依描述產生一個 AI 分解 tab 設定（label/system/query/placeholder）。"""
        from ..planner import suggest_tab as _suggest_tab
        return _suggest_tab(description, self.settings)

    # 子用例自動分析上限：避免單一主用例衍生過多子用例淹沒看板（被截斷時會明示截斷數）。
    # 已多級分組 + 含目錄擴充，上限放寬；每步 LLM 補強另有上限見 _MAX_GAP_PER_STEP。
    _MAX_CHILDREN = 24
    _MAX_GAP_PER_STEP = 6

    def _action_code(self, child: dict) -> int | None:
        """從子用例 spec 取「負向動作」的錯誤碼（path 末段帶 expect.code 的步驟）。無則 None。"""
        for st in reversed(child.get("path", [])):
            code = (st.get("expect") or {}).get("code")
            if isinstance(code, int):
                return code
        return None

    def _annotate_child(self, child: dict, actor: str | None, recommended: bool) -> dict:
        """把一條子用例 spec 包成前端用的卡片：附錯誤碼/碼名/領域分組/是否推薦勾選 + spec_yaml。"""
        from ..api_error_codes import describe, group_for

        code = self._action_code(child)
        info = describe(code) if code is not None else {"name": "", "label": ""}
        gkey, glabel = group_for(code, actor)
        return {
            "id": child.get("id", ""),
            "description": str(child.get("description", "")).strip(),
            "skip": bool(child.get("skip")),
            "skip_reason": child.get("skip_reason", ""),
            "code": code,
            "code_name": info["name"],
            "code_label": info["label"],
            "group_key": gkey,
            "group_label": glabel,
            "recommended": recommended,
            "spec_yaml": yaml.safe_dump(child, allow_unicode=True, sort_keys=False),
        }

    def _derive_children(self, main_spec: dict) -> dict:
        """從主 spec 推導子用例（分支 / 邊界 / 負向），結合完整錯誤碼目錄分多級。

        兩個來源：
          - **已建模 branches**（endpoints.yaml）：對每個有 branches 的 target 呼叫
            case_gen.generate(target, "L1") 取 negative（丟 happy）。可跑者標 recommended=True
            （前端預設勾選）；因時間鎖/缺帳號等 skip 者 recommended=False（覆蓋缺口，預設不勾）。
          - **目錄擴充**（planner.suggest_code_gaps，需 DEEPSEEK_API_KEY）：對照 ApiExceptionCode
            完整目錄，補出「該步可能觸發但 endpoints.yaml 尚未建模」的負向碼，合成 skip 佔位子用例
            （recommended=False；標「尚未建模」覆蓋缺口）。無 key / 失敗則靜默跳過，不影響主分解。

        每條子用例經 _annotate_child 附錯誤碼/碼名/領域分組（商家端/打工夥伴/承攬制/共通/系統）。
        回 {children:[...annotated], analyzed, truncated}；children 仍為**扁平**陣列（保 commit 相容），
        分組第一層由前端依 group_key 自行收攏。
        """
        from ..case_gen import _branch_case, generate
        from ..planner import suggest_code_gaps
        from ..registry import unit_spec

        # 1) 掃出有 branches 的 target，連帶記下其 actor / 已建模碼（供分組與 LLM 補強）
        targets: list[str] = []
        actor_of: dict[str, str | None] = {}
        modeled_codes: dict[str, set[int]] = {}
        steps_meta: list[dict] = []
        seen_targets: set[str] = set()
        for st in main_spec.get("path", []):
            name = st.get("transition")
            if not name or name in seen_targets:
                continue
            seen_targets.add(name)
            try:
                u = unit_spec(name)
            except Exception:  # noqa: BLE001 — 找不到單元規格者跳過
                continue
            branches = u.get("branches") or []
            if not branches:
                continue
            targets.append(name)
            actor_of[name] = u.get("actor")
            codes = {(b.get("expect_fail") or {}).get("code")
                     for b in branches if isinstance((b.get("expect_fail") or {}).get("code"), int)}
            modeled_codes[name] = codes
            steps_meta.append({"transition": name, "system": u.get("system"),
                               "actor": u.get("actor"), "summary": u.get("summary"),
                               "endpoint": u.get("endpoint"), "modeled_codes": sorted(codes)})

        children: list[dict] = []
        seen_ids: set[str] = set()

        # 2) 已建模 branches → negative 子用例（依 branch 順序對齊，取 expect_fail 當主碼）
        for target in targets:
            negs = [c for c in generate(target, "L1") if not c.get("id", "").endswith("-happy")]
            branches = unit_spec(target).get("branches") or []
            for br, child in zip(branches, negs):
                cid = child.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                children.append(self._annotate_child(
                    child, actor_of.get(target), recommended=not child.get("skip")))

        # 3) 目錄擴充：LLM 對照完整錯誤碼目錄補「尚未建模」的負向碼（失敗回 {}，不影響上面）
        gaps = suggest_code_gaps(steps_meta, self.settings)
        for target, items in gaps.items():
            base = len(unit_spec(target).get("branches") or [])
            for i, it in enumerate(items[:self._MAX_GAP_PER_STEP]):
                when = it.get("when") or "（AI 補出的負向情境）"
                fake_br = {
                    "when": when,
                    "expect_fail": {"code": it["code"]},
                    # arrange.note 會讓 _branch_case 標 skip：尚未建模 → 作可見覆蓋缺口
                    "arrange": {"note": f"AI 對照錯誤碼目錄補出（endpoints.yaml 尚未建模 {it['code']}）"},
                }
                child = _branch_case(target, fake_br, base + i)
                cid = child.get("id", "")
                if cid in seen_ids:
                    continue
                seen_ids.add(cid)
                children.append(self._annotate_child(
                    child, actor_of.get(target), recommended=False))

        analyzed = len(children)
        truncated = max(0, analyzed - self._MAX_CHILDREN)
        if truncated:
            children = children[:self._MAX_CHILDREN]
        return {"children": children, "analyzed": analyzed, "truncated": truncated}

    # 分解描述中的「#數字」視為看板序號（qa_cases.seq）引用既有用例，
    # 例：「發佈一條 #2191 一樣的流程，只是把工作開始時間改為明天下午13點」。
    _CASE_REF = re.compile(r"#(\d+)")

    def _expand_case_refs(self, use_case: str) -> tuple[str, list[dict]]:
        """把分解描述中的 #N 引用展開成「引用用例定義」附錄，一併餵給 LLM 當分解基準。

        每個 #N 以 qa_cases.seq 反查用例 id，再讀其 YAML 定義（_find，以檔案為準）
        附在描述後。找不到對應序號時直接拋錯——靜默忽略會讓 LLM 看不懂 #N 而亂分解。
        回傳 (展開後文字, refs)；refs 為 [{seq, id}]，附在 preview 回傳供前端顯示。
        """
        refs: list[dict] = []
        blocks: list[str] = []
        seen: set[int] = set()
        for m in self._CASE_REF.finditer(use_case):
            seq = int(m.group(1))
            if seq in seen:
                continue
            seen.add(seq)
            cid = self.qa.case_id_by_seq(seq)
            found = self._find(cid) if cid else None
            if found is None:
                raise ValueError(f"描述引用了 #{seq}，但找不到對應的用例（看板序號不存在）")
            _, _, spec = found
            refs.append({"seq": seq, "id": cid})
            blocks.append(f"## 引用用例 #{seq}（id: {cid}）的既有定義（YAML）：\n"
                          + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
        if not blocks:
            return use_case, refs
        expanded = (use_case
                    + "\n\n# 引用用例定義\n描述中的 #N 指的就是下列既有用例。"
                    "請以其任務流為基準分解，再依描述要求的差異調整。\n\n"
                    + "\n".join(blocks))
        return expanded, refs

    def decompose_preview(self, use_case: str, system: str | None = None) -> dict:
        """分解第一段：呼叫 LLM 產 plan + 展開 spec，算好防撞 id，但**不落地**。

        刻意只做純計算（planner 分解 → build_path → _unique_case_id），
        全程不 mkdir / 不 write_text / 不 sync_cases / 不 _run_spec —— 取消時不留任何記錄。
        回傳同時附 spec 的 YAML 字串（spec_yaml），供前端塞進可編輯 textarea。
        使用者確認/校正後再以 decompose_commit 真正建立。
        """
        from ..planner import build_path
        from ..planner import decompose as _decompose

        # 描述中的 #N 先展開成既有用例定義（找不到會直接拋錯，不送 LLM）
        use_case, refs = self._expand_case_refs(use_case)
        # system 為前端 tab 指定的目標系統（job/contract）；透傳給 planner
        plan = _decompose(use_case, self.settings, system=system)
        spec = build_path(plan)
        spec["id"] = self._unique_case_id(str(spec.get("id") or "ai-case"))  # 預先算好防撞號
        spec_yaml = yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
        # 一併分析可能的子用例（分支 / 邊界 / 負向）——同樣只算不落地，供前端彈窗勾選。
        # _derive_children 已把每條附上錯誤碼 / 領域分組 / recommended，children 維持扁平。
        from ..api_error_codes import GROUP_LABELS, GROUP_ORDER
        derived = self._derive_children(spec)
        return {"plan": plan.raw, "spec": spec, "spec_yaml": spec_yaml,
                "proposed_id": spec["id"], "system": plan.system,
                "refs": refs,
                "children": derived["children"],
                "group_order": GROUP_ORDER,
                "group_labels": GROUP_LABELS,
                "children_analyzed": derived["analyzed"],
                "children_truncated": derived["truncated"]}

    def decompose_commit(self, spec: dict | str, run: bool = False,
                         children: list[dict | str] | None = None) -> dict:
        """分解第二段：把（可能經前端校正過的）spec 真正落地。

        spec 可為 dict 或 YAML 字串（前端送 textarea 內容時為字串）。
        落地流程：解析 → 對最終 id 再做一次 _unique_case_id 防撞（preview→commit
        之間可能有人佔號）→ mkdir + write_text + sync_cases；run 為真才 _run_spec。

        children（可選，預設 None）：使用者在彈窗勾選保留的子用例（dict 或 YAML 字串）。
          預設 None 時整段不執行——確保 copy_case / republish_case 等既有呼叫行為完全不變。
          有帶時：先落地主 spec 取得主用例**最終 id**，再對每條子用例綁 parent=主最終 id、
          取防撞 id、寫檔 + sync_cases（**不執行**子用例，run 只作用於主用例）。
          子用例若帶 skip=True 仍照樣落地成記錄（讓覆蓋缺口可見），它本就標記不可跑。

        回傳形狀與舊 decompose 相容：{plan?, spec, saved, system, result?}；
        有帶 children 時多回 children: [{id, saved, system, skip}]。
        """
        if isinstance(spec, str):
            parsed = yaml.safe_load(spec)
            if not isinstance(parsed, dict) or "path" not in parsed:
                raise ValueError("spec YAML 格式不正確（需為含 path 的物件）")
            spec = parsed
        # commit 時對最終 id 再防撞一次（preview 算過的號到此刻可能已被別人佔用）
        spec["id"] = self._unique_case_id(str(spec.get("id") or "ai-case"))
        system = _detect_system(spec)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out = GENERATED_DIR / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        payload = {"spec": spec, "saved": out.name, "system": system}
        if run:
            # _run_spec 內部會 sync_cases 再執行
            payload["result"] = self._run_spec(spec, source="generated", file=out.name)
        else:
            # 不執行時也要把用例註冊進 qa_cases（與 list_cases / _run_spec 的 sync 一致）
            self.qa.sync_cases([{
                "id": spec["id"], "file": out.name, "system": system, "source": "generated",
                "description": str(spec.get("description", "")).strip(),
                "step_count": len(spec.get("path", [])),
                "yaml": yaml.safe_dump(spec, allow_unicode=True, sort_keys=False),
                "created_at": 0,
            }])
        # 子用例：在主用例落地、拿到最終 id 之後才綁 parent 與防撞 id（主 id 可能因防撞而變）
        if children:
            payload["children"] = self._commit_children(children, spec["id"])
        return payload

    def _commit_children(self, children: list[dict | str], parent_id: str) -> list[dict]:
        """把使用者勾選保留的子用例綁到主用例最終 id 後落地（不執行）。

        每條 child：解析（dict 或 YAML 字串）→ 設 parent=parent_id →
        以原 id（或 ai-subcase 基底）取防撞 id → 寫 generated/{id}.yaml + sync_cases。
        子用例不執行（run 只作用於主用例）；之後可由使用者自行執行 / 重新發佈。
        """
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out_list: list[dict] = []
        for child in children:
            if isinstance(child, str):
                parsed = yaml.safe_load(child)
                if not isinstance(parsed, dict) or "path" not in parsed:
                    raise ValueError("子用例 spec YAML 格式不正確（需為含 path 的物件）")
                child = parsed
            child["parent"] = parent_id                               # 綁到主用例最終 id
            child["id"] = self._unique_case_id(str(child.get("id") or "ai-subcase"))
            csys = _detect_system(child)
            cpath = GENERATED_DIR / f"{child['id']}.yaml"
            cyaml = yaml.safe_dump(child, allow_unicode=True, sort_keys=False)
            cpath.write_text(cyaml, encoding="utf-8")
            # 註冊進 qa_cases（帶 parent_id），主列才會即時長出「子任務(n)」入口
            self.qa.sync_cases([{
                "id": child["id"], "file": cpath.name, "system": csys, "source": "generated",
                "description": str(child.get("description", "")).strip(),
                "step_count": len(child.get("path", [])),
                "yaml": cyaml, "created_at": 0, "parent_id": parent_id,
            }])
            out_list.append({"id": child["id"], "saved": cpath.name,
                             "system": csys, "skip": bool(child.get("skip"))})
        return out_list

    def decompose(self, use_case: str, run: bool = False,
                  system: str | None = None) -> dict:
        """一步到位的舊行為（preview + commit 組合），供 CLI / 既有呼叫端沿用。

        新看板流程改走 decompose_preview（彈窗確認）+ decompose_commit（確認後落地）；
        此方法保留是為了不破壞任何依賴舊「分解即落地」語意的呼叫端。
        """
        pv = self.decompose_preview(use_case, system=system)
        out = self.decompose_commit(pv["spec"], run=run)
        # 補回舊 decompose 會帶的 plan（commit 不重算 plan，從 preview 取）
        out["plan"] = pv["plan"]
        return out
