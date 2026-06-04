"""測試用例層：列出 cases/*.yaml、對應 results/*.json，並承接「執行」與「AI 分解」。

純檢視 + 觸發：
  - list/detail 只讀 YAML 與 results JSON。
  - run_case / decompose 會真的登入 + 呼叫被測 API（同步），複用既有 autotest/planner 管線。
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ..config import Settings

ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = ROOT / "cases"
GENERATED_DIR = CASES_DIR / "generated"
RESULTS_DIR = ROOT / "results"


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


def _results_index() -> dict[str, list[tuple[int, Path]]]:
    """path_id → [(started_at, json_path), ...]（新到舊）。檔名格式 <path_id>-<ts>.json。"""
    idx: dict[str, list[tuple[int, Path]]] = {}
    if not RESULTS_DIR.is_dir():
        return idx
    for p in RESULTS_DIR.glob("*.json"):
        pid, _, ts = p.stem.rpartition("-")
        if not pid or not ts.isdigit():
            continue
        idx.setdefault(pid, []).append((int(ts), p))
    for pid in idx:
        idx[pid].sort(reverse=True)
    return idx


def _load_yaml(path: Path) -> dict | None:
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — 壞檔略過
        return None
    return spec if isinstance(spec, dict) and "path" in spec else None


def _result_summary(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    steps = d.get("steps", [])
    return {
        "status": d.get("status"),
        "started_at": d.get("started_at"),
        "passed": sum(1 for s in steps if s.get("status") == "passed"),
        "total": len(steps),
        "failed_at": d.get("failed_at"),
    }


class CaseStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()

    # ── 清單 ─────────────────────────────────────────────────────────────────
    def list_cases(self, system: str | None = None, q: str = "",
                   limit: int = 20, offset: int = 0) -> dict:
        ridx = _results_index()
        items = []
        ql = q.lower().strip()
        for path, source in _case_files():
            spec = _load_yaml(path)
            if spec is None:
                continue
            sysname = _detect_system(spec)
            if system and sysname != system:
                continue
            cid = spec.get("id", path.stem)
            desc = str(spec.get("description", "")).strip()
            if ql and ql not in (cid + " " + desc).lower():
                continue
            try:
                created = int(path.stat().st_mtime)
            except OSError:
                created = 0
            res = ridx.get(cid, [])
            items.append({
                "id": cid,
                "file": path.name,
                "description": desc,
                "system": sysname,
                "source": source,
                "created_at": created,
                "step_count": len(spec.get("path", [])),
                "transitions": _transitions(spec),
                "run_count": len(res),
                "last_result": _result_summary(res[0][1]) if res else None,
            })
        # 依建立時間（檔案 mtime）倒序；同時間以 id 穩定排序
        items.sort(key=lambda x: x["id"])
        items.sort(key=lambda x: x["created_at"], reverse=True)
        total = len(items)
        page = items[offset:offset + limit] if limit else items
        return {"items": page, "total": total}

    # ── 詳情 ─────────────────────────────────────────────────────────────────
    def _find(self, case_id: str) -> tuple[Path, str, dict] | None:
        for path, source in _case_files():
            spec = _load_yaml(path)
            if spec is not None and spec.get("id", path.stem) == case_id:
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
        ridx = _results_index().get(case_id, [])
        last_full = None
        if ridx:
            try:
                last_full = json.loads(ridx[0][1].read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                last_full = None
        return {
            "id": case_id, "file": path.name, "source": source,
            "system": _detect_system(spec),
            "description": str(spec.get("description", "")).strip(),
            "steps": steps,
            "yaml": path.read_text(encoding="utf-8"),
            "history": [_result_summary(p) for _, p in ridx[:10]],
            "last_result": last_full,
        }

    # ── 執行 / 分解（會真的打被測 API）────────────────────────────────────────
    def _run_spec(self, spec: dict) -> dict:
        from ..autotest import _actors_for
        from ..recorder import RecordingRunner
        from ..verifier import DBVerifier

        system = _detect_system(spec)
        db = DBVerifier(self.settings.for_system(system))   # contract/job dev 分庫
        actors = _actors_for(system, self.settings)
        return RecordingRunner(db).run(spec, actors=actors).to_dict()

    def run_case(self, case_id: str) -> dict:
        found = self._find(case_id)
        if found is None:
            raise ValueError(f"找不到用例 {case_id}")
        return self._run_spec(found[2])

    def decompose(self, use_case: str, run: bool = False) -> dict:
        from ..planner import build_path
        from ..planner import decompose as _decompose

        plan = _decompose(use_case, self.settings)
        spec = build_path(plan)
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        out = GENERATED_DIR / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        payload = {"plan": plan.raw, "spec": spec, "saved": out.name, "system": plan.system}
        if run:
            payload["result"] = self._run_spec(spec)
        return payload
