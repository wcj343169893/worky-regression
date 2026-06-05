"""測試用例層：列出 cases/*.yaml，執行結果落地 / 讀取 worky_qa_dashboard，並承接「執行」與「AI 分解」。

純檢視 + 觸發：
  - list/detail 讀 YAML（用例定義）+ qa_store（執行結果）。
  - run_case / decompose 會真的登入 + 呼叫被測 API（同步），複用既有 autotest/planner 管線，
    執行結果寫入 worky_qa_dashboard。
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ..config import Settings
from ..qa_store import QAStore

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "cases"
GENERATED_DIR = CASES_DIR / "generated"


def _detect_system(spec: dict) -> str:
    """以第一個 transition 前綴判系統：J* → job，T* → contract，A* → activity。"""
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


def _load_yaml(path: Path) -> dict | None:
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — 壞檔略過
        return None
    return spec if isinstance(spec, dict) and "path" in spec else None


def _case_id(spec: dict, path: Path) -> str:
    """用例 id：有 id 用 id，否則用檔名 stem（永不 unnamed，保證可追溯）。"""
    return str(spec.get("id") or path.stem)


def _case_record(path: Path, source: str, spec: dict) -> dict:
    """組 qa_cases upsert / 清單共用的用例基本資料。"""
    try:
        created = int(path.stat().st_mtime)
    except OSError:
        created = 0
    # 父用例 id 來自 spec 的 parent 欄；無則頂層（None）
    parent = spec.get("parent")
    return {
        "id": _case_id(spec, path),
        "file": path.name,
        "system": _detect_system(spec),
        "source": source,
        "description": str(spec.get("description", "")).strip(),
        "step_count": len(spec.get("path", [])),
        "yaml": path.read_text(encoding="utf-8"),
        "created_at": created,
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
        } for rec, spec in records]
        # 依建立時間（檔案 mtime）倒序；同時間以 id 穩定排序
        items.sort(key=lambda x: x["id"])
        items.sort(key=lambda x: x["created_at"], reverse=True)
        total = len(items)
        page = items[offset:offset + limit] if limit else items
        # 每頁項目的序號與執行彙總從 DB 取（只查當頁，避免大量查詢）
        for it in page:
            it["seq"] = self.qa.case_seq(it["id"])
            it["last_result"] = self.qa.latest_summary(it["id"])
            it["run_count"] = self.qa.run_count(it["id"])
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
        if found is None:
            return None
        path, source, spec = found
        steps = []
        for i, st in enumerate(spec.get("path", [])):
            if "db_exec" in st:
                steps.append({"index": i, "kind": "db_exec", "name": "db_exec",
                              "sql": st["db_exec"], "flush_cache": st.get("flush_cache", False)})
            else:
                steps.append({"index": i, "kind": "transition", "name": st.get("transition", "?"),
                              "save": st.get("save"), "expect": st.get("expect")})
        return {
            "id": case_id, "file": path.name, "source": source,
            "system": _detect_system(spec),
            "description": str(spec.get("description", "")).strip(),
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
        res_trans = [s for s in (last or {}).get("steps", []) if s.get("kind") == "transition"]
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
                  actors: dict | None = None) -> dict:
        from ..autotest import _actors_for
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
        db = DBVerifier(self.settings.for_system(system))   # contract/job dev 分庫
        # actors 可由呼叫端覆蓋（換號時帶入「已排除原帳號」的 actors）；否則照常配發
        if actors is None:
            actors = _actors_for(system, self.settings)
        return RecordingRunner(db, qa_store=self.qa, system=system).run(spec, actors=actors).to_dict()

    def run_case(self, case_id: str) -> dict:
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        path, source, spec = found
        return self._run_spec(spec, source=source, file=path.name)

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
        from ..autotest import _actors_for
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
        before = _actors_for(system, self.settings)
        cur = before.get(actor_name) or before.get(role)
        cur_id = getattr(cur, "user_id", None)
        if cur_id is None:
            raise ValueError(f"無法判定 actor「{actor_name}」目前使用的帳號")
        try:
            swapped = _actors_for(system, self.settings, exclude={role: [str(cur_id)]})
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

    def suggest_tab(self, description: str) -> dict:
        """依描述產生一個 AI 分解 tab 設定（label/system/query/placeholder）。"""
        from ..planner import suggest_tab as _suggest_tab
        return _suggest_tab(description, self.settings)

    def decompose(self, use_case: str, run: bool = False,
                  system: str | None = None) -> dict:
        from ..planner import build_path
        from ..planner import decompose as _decompose

        # system 為前端 tab 指定的目標系統（job/contract）；透傳給 planner
        plan = _decompose(use_case, self.settings, system=system)
        spec = build_path(plan)
        spec["id"] = self._unique_case_id(str(spec.get("id") or "ai-case"))  # 防覆蓋
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out = GENERATED_DIR / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        payload = {"plan": plan.raw, "spec": spec, "saved": out.name, "system": plan.system}
        if run:
            payload["result"] = self._run_spec(spec, source="generated", file=out.name)
        return payload
