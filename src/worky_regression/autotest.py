"""Layer ③ CLI — 後台跑「用例 → 任務流 → 執行 → 記錄結果」整條管線。

    # 用自然語言用例（需 DEEPSEEK_API_KEY）
    python -m worky_regression.autotest "商家發工作，夥伴申請後商家取消錄取"

    # 只分解、不執行（看產出的任務流 YAML）
    python -m worky_regression.autotest "..." --dry-run

    # 跳過分解，直接跑手寫/先前產出的 lean plan
    python -m worky_regression.autotest --plan plan.json

    # 直接跑既有的 path YAML（不分解）
    python -m worky_regression.autotest --path cases/job-happy-core.yaml

產出：cases/generated/<path_id>.yaml（產生的任務流，可檢視/編輯）
      results/<path_id>-<ts>.json（執行結果記錄）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .actor import Actor
from .client import WorkyClient
from .config import Settings
from .planner import TaskPlan, build_path, decompose, plan_to_json
from .recorder import RecordingRunner
from .verifier import DBVerifier

ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS = ROOT / "cases" / "_fixtures" / "test_accounts.yaml"
GENERATED = ROOT / "cases" / "generated"


def _build_actor(s: Settings, accounts: dict, role: str, key: str, user_type: int) -> Actor:
    cfg = accounts[key]
    client = WorkyClient(s, user_type=user_type)
    actor = Actor(role=role, user_type=user_type, phone=cfg["phone"],
                  user_id=cfg["id"], client=client, shop_id=cfg.get("shop_id"))
    actor.login(audit_code=s.audit_sms_code)
    return actor


def ensure_publisher_invoice(actor: Actor) -> None:
    """確保承攬制 publisher 已設定發票資訊，否則 /contract/task/publish 會 throw 50045。

    以 audit publisher 身份呼叫 /contract/invoice/update 寫入最小設定（type=0 捐贈發票）。
    Idempotent：覆寫舊值不會壞事。conftest 與 autotest/dashboard 共用此 preflight。
    """
    resp = actor.client.post(
        "/contract/invoice/update",
        body={
            "type": 0,                          # 捐贈發票
            "name": "regression",
            "phone": actor.phone,
            "email": "regression@worky.local",
            "e_invoice_carrier_type": 0,        # 無載具（捐贈用）
            "mobile_carrier_number": "",
            "citizen_carrier_number": "",
            "tax_id_number": "",
            "tax_id_number_title": "",
        },
    )
    if resp.status_code != 200 or resp.json().get("success") is False:
        raise RuntimeError(
            f"failed to setup invoice for publisher id={actor.user_id}: {resp.text[:300]}"
        )


def _actors_for(system: str, s: Settings) -> dict[str, Actor]:
    """依系統登入對應角色（承攬制 publisher 會做發票 preflight）。"""
    accounts = yaml.safe_load(ACCOUNTS.read_text(encoding="utf-8"))
    if system == "job":
        return {
            "employer": _build_actor(s, accounts, "employer", "employer_primary", 1),
            "labor": _build_actor(s, accounts, "labor", "publisher_primary", 2),
        }
    publisher = _build_actor(s, accounts, "publisher", "publisher_primary", 2)
    ensure_publisher_invoice(publisher)
    return {
        "publisher": publisher,
        "receiver": _build_actor(s, accounts, "receiver", "receiver_primary", 2),
    }


def _load_plan(path: Path) -> TaskPlan:
    data = json.loads(path.read_text(encoding="utf-8"))
    return TaskPlan(path_id=data["path_id"], description=data["description"],
                    system=data["system"], steps=data["steps"], raw=data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="worky-autotest", description="用例 → 任務流 → 執行 → 記錄")
    ap.add_argument("use_case", nargs="?", help="自然語言用例")
    ap.add_argument("--plan", type=Path, help="跳過分解，讀 lean plan JSON")
    ap.add_argument("--path", type=Path, help="直接跑既有 path YAML（不分解）")
    ap.add_argument("--dry-run", action="store_true", help="只產生任務流，不執行")
    ap.add_argument("--no-save", action="store_true", help="不寫 cases/generated/")
    args = ap.parse_args(argv)

    s = Settings.from_env()

    # 1) 取得 path dict（三種來源）
    if args.path:
        spec = yaml.safe_load(args.path.read_text(encoding="utf-8"))
        system = "job" if any("J" == str(st.get("transition", "?"))[:1]
                              for st in spec["path"]) else "contract"
        print(f"▶ 跑既有 path：{args.path}")
    else:
        if args.plan:
            plan = _load_plan(args.plan)
            print(f"▶ 讀 lean plan：{args.plan}")
        elif args.use_case:
            print(f"▶ 分解用例：{args.use_case}")
            plan = decompose(args.use_case, s)
        else:
            ap.error("需要 use_case，或 --plan，或 --path 之一")
            return 2
        print(f"  系統={plan.system}  path_id={plan.path_id}")
        print(f"  描述={plan.description}")
        for i, st in enumerate(plan.steps):
            label = st.get("transition") or f"db_exec: {st.get('sql', '')[:50]}"
            note = f"  # {st['note']}" if st.get("note") else ""
            print(f"   {i}. [{st['kind']}] {label}{note}")
        spec = build_path(plan)
        system = plan.system

    # 2) 落地產生的任務流 YAML（可檢視/編輯/重跑）
    if not args.path and not args.no_save:
        GENERATED.mkdir(parents=True, exist_ok=True)
        out = GENERATED / f"{spec['id']}.yaml"
        out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"  ↳ 任務流已寫入 {out}")

    if args.dry_run:
        print("\n--- 產生的 path（--dry-run，未執行）---")
        print(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False))
        return 0

    # 3) 登入角色 + 執行 + 記錄（contract 與 job 在 dev 分庫，依系統選 DB）
    db = DBVerifier(s.for_system(system))
    actors = _actors_for(system, s)
    result = RecordingRunner(db).run(spec, actors=actors)

    print(f"\n{'='*60}")
    print(result.summary())
    for st in result.steps:
        mark = {"passed": "✓", "failed": "✗", "skipped": "·"}.get(st.status, "?")
        extra = ""
        if st.observations.get("saved"):
            extra = f"  saved={st.observations['saved']}"
        elif st.error:
            extra = f"  {st.error}"
        print(f"  {mark} [{st.index}] {st.name:30s} {st.elapsed_ms:>5}ms{extra}")
    print(f"{'='*60}")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
