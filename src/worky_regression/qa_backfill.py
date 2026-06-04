"""一次性匯入指令：把現有 results/*.json 歷史執行紀錄灌進 worky_qa_dashboard。

    python -m worky_regression.qa_backfill

流程：
  1. 確保 schema 到最新（alembic upgrade head）。
  2. 掃 cases/*.yaml + generated/*.yaml，註冊進 qa_cases。
  3. 掃 results/*.json，逐檔寫入 qa_runs / qa_run_steps（run_id = 檔名 stem，source='import'）。
冪等：同 run_id 重跑會覆蓋。原 JSON 檔保留在磁碟，不刪。
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import Settings
from .dashboard.cases import _case_files, _case_record, _load_yaml
from .qa_store import QAStore

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "results"


def _sync_all_cases(qa: QAStore) -> int:
    records = []
    for path, source in _case_files():
        spec = _load_yaml(path)
        if spec is None:
            continue
        records.append(_case_record(path, source, spec))
    qa.sync_cases(records)
    return len(records)


def _import_results(qa: QAStore) -> tuple[int, int, list[str]]:
    runs = steps = 0
    skipped: list[str] = []
    if not RESULTS_DIR.is_dir():
        return runs, steps, skipped
    for p in sorted(RESULTS_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            skipped.append(f"{p.name}（壞檔：{e}）")
            continue
        case_id = d.get("path_id")
        if not case_id:
            skipped.append(f"{p.name}（缺 path_id）")
            continue
        st = d.get("steps", [])
        # 從檔名推系統前綴：J* → job，其餘 contract
        first = next((s.get("name", "") for s in st if s.get("kind") == "transition"), "")
        system = "job" if first[:1] == "J" else "contract"
        qa.insert_run(
            run_id=p.stem,                       # 檔名本就唯一（<case_id>-<ts>）
            case_id=case_id,
            system=system,
            status=d.get("status", ""),
            description=str(d.get("description", "")).strip(),
            started_at=int(d.get("started_at", 0)),
            failed_at=d.get("failed_at"),
            steps=st,
            source="import",
        )
        runs += 1
        steps += len(st)
    return runs, steps, skipped


def main() -> int:
    settings = Settings.from_env()
    qa = QAStore(settings)
    print(f"→ 確保 schema（{settings.qa_db_name} @ {settings.db_host}）…")
    qa.migrate()
    ncases = _sync_all_cases(qa)
    print(f"→ 已註冊用例：{ncases} 筆")
    nruns, nsteps, skipped = _import_results(qa)
    print(f"→ 匯入執行紀錄：{nruns} 次、{nsteps} 步")
    if skipped:
        print(f"→ 略過 {len(skipped)} 檔：")
        for s in skipped:
            print(f"   - {s}")
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
