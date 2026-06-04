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
    """以第一個 transition 前綴判系統：J* → job，T* → contract。"""
    for st in spec.get("path", []):
        t = str(st.get("transition", ""))
        if t[:1] == "J":
            return "job"
        if t[:1] == "T":
            return "contract"
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
    return {
        "id": _case_id(spec, path),
        "file": path.name,
        "system": _detect_system(spec),
        "source": source,
        "description": str(spec.get("description", "")).strip(),
        "step_count": len(spec.get("path", [])),
        "yaml": path.read_text(encoding="utf-8"),
        "created_at": created,
    }


class CaseStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self.qa = QAStore(self.settings)

    # ── 清單 ─────────────────────────────────────────────────────────────────
    def list_cases(self, system: str | None = None, q: str = "",
                   limit: int = 20, offset: int = 0) -> dict:
        ql = q.lower().strip()
        records: list[tuple[dict, dict]] = []   # (case_record, spec)
        for path, source in _case_files():
            spec = _load_yaml(path)
            if spec is None:
                continue
            rec = _case_record(path, source, spec)
            if system and rec["system"] != system:
                continue
            if ql and ql not in (rec["id"] + " " + rec["description"]).lower():
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
            "transitions": _transitions(spec),
        } for rec, spec in records]
        # 依建立時間（檔案 mtime）倒序；同時間以 id 穩定排序
        items.sort(key=lambda x: x["id"])
        items.sort(key=lambda x: x["created_at"], reverse=True)
        total = len(items)
        page = items[offset:offset + limit] if limit else items
        # 每頁項目的執行彙總從 DB 取（只查當頁，避免大量查詢）
        for it in page:
            it["last_result"] = self.qa.latest_summary(it["id"])
            it["run_count"] = self.qa.run_count(it["id"])
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
    def _run_spec(self, spec: dict, *, source: str = "builtin", file: str = "") -> dict:
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
        actors = _actors_for(system, self.settings)
        return RecordingRunner(db, qa_store=self.qa, system=system).run(spec, actors=actors).to_dict()

    def run_case(self, case_id: str) -> dict:
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        path, source, spec = found
        return self._run_spec(spec, source=source, file=path.name)

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

    def decompose(self, use_case: str, run: bool = False) -> dict:
        from ..planner import build_path
        from ..planner import decompose as _decompose

        plan = _decompose(use_case, self.settings)
        spec = build_path(plan)
        spec["id"] = self._unique_case_id(str(spec.get("id") or "ai-case"))  # 防覆蓋
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out = GENERATED_DIR / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        payload = {"plan": plan.raw, "spec": spec, "saved": out.name, "system": plan.system}
        if run:
            payload["result"] = self._run_spec(spec, source="generated", file=out.name)
        return payload
