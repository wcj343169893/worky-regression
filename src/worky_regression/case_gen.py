"""依 coverage level 從 endpoints.yaml 的 guards/branches 自動產用例。

把「結構化的 form rule」展開成可跑的任務流：
- **L0 happy**：沿 guards.satisfy.by 推出前置 transition 鏈，注入 guards 的 db_exec setup，
  最後接目標 transition（正向 expect 由 push/side_effects 自動推導）。
- **L1 negative**：對目標 transition 的每個 branch 產一條負向子用例＝happy 前置 + branch.arrange
  + 目標(expect.success=false + code/message)。

arrange 可自動化的：`db_exec`（前置 SQL）、`by`（跑前序 transition 造前提）。
不可自動化的：`caps_lacking`（需缺特定能力的帳號）、`note`（需特殊安排）→ 標記為 skip 並附原因，
讓覆蓋缺口「可見」而非靜默略過。

CLI：
    python -m worky_regression.case_gen J2_labor_apply --level L1            # 印出產生的用例
    python -m worky_regression.case_gen J2_labor_apply --level L1 --write    # 寫入 cases/generated/
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .planner import _expect_from_unit
from .registry import unit_spec

GENERATED = Path(__file__).resolve().parents[2] / "cases" / "generated"


def _guard_db_execs(name: str) -> list[str]:
    out = []
    for g in (unit_spec(name).get("guards") or []):
        sql = (g.get("satisfy") or {}).get("db_exec")
        if sql:
            out.append(sql)
    return out


def _prereq_chain(target: str) -> list[str]:
    """沿 guards.satisfy.by 推出抵達 target 前要先跑的 transition（依賴序、去重，不含 target）。"""
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        for g in (unit_spec(name).get("guards") or []):
            by = (g.get("satisfy") or {}).get("by")
            if by and by not in seen:
                seen.add(by)
                visit(by)
                order.append(by)

    visit(target)
    return order


def _happy_step(name: str) -> dict[str, Any]:
    u = unit_spec(name)
    step: dict[str, Any] = {"transition": name}
    if u.get("saves"):
        step["save"] = dict(u["saves"])
    step["expect"] = _expect_from_unit(name)
    return step


def _setup_steps(name: str) -> list[dict[str, Any]]:
    """某 transition 的 guards 轉成的 db_exec 前置步驟（caps/by 不在此，分別由帳號池/前序處理）。"""
    return [{"db_exec": sql, "expect_min_affected": 0} for sql in _guard_db_execs(name)]


def _prefix(target: str) -> list[dict[str, Any]]:
    """抵達 target「動作前」的所有步驟：前序 transition（各帶自身 guard setup）+ target 的 guard setup。"""
    steps: list[dict[str, Any]] = []
    for name in _prereq_chain(target):
        steps += _setup_steps(name)
        steps.append(_happy_step(name))
    steps += _setup_steps(target)
    return steps


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z一-鿿]+", "-", text).strip("-")
    return s[:32] or "br"


def _negative_action(target: str, br: dict[str, Any]) -> dict[str, Any]:
    ef = br.get("expect_fail") or {}
    expect: dict[str, Any] = {"success": False}
    if ef.get("code") is not None:
        expect["code"] = ef["code"]
    if ef.get("message_contains"):
        expect["message_contains"] = ef["message_contains"]
    return {"transition": target, "expect": expect}


def _branch_case(target: str, br: dict[str, Any], idx: int) -> dict[str, Any]:
    arrange = br.get("arrange") or {}
    steps = _prefix(target)
    skip_reasons: list[str] = []

    # by：跑前序 transition 造出該前提（如「已申請過」= 先 J2 一次）
    for extra in (arrange.get("by") or []):
        steps.append(_happy_step(extra))
    # 同一 actor 連續呼叫會觸發後端 9002「執行操作過快」(~1s 節流)，arrange.by 後補 sleep
    if arrange.get("by"):
        steps.append({"sleep": 2})
    # db_exec：交易級前置
    if arrange.get("db_exec"):
        steps.append({"db_exec": arrange["db_exec"]})
    # 不可自動化：標記 skip（覆蓋缺口可見）
    if arrange.get("caps_lacking"):
        skip_reasons.append(f"需配缺能力 {arrange['caps_lacking']} 的帳號（待帳號池提供 deficiency actor）")
    if arrange.get("note"):
        skip_reasons.append(f"需特殊安排：{arrange['note']}")

    steps.append(_negative_action(target, br))
    case: dict[str, Any] = {
        "id": f"gen-{target}-neg{idx}-{_slug(br.get('when', ''))}",
        "description": f"[L1 negative] {unit_spec(target).get('summary', target)}：{br.get('when', '')}",
        "vars": {"job_recruit_count": 1},
        "path": steps,
    }
    if skip_reasons:
        case["skip"] = True
        case["skip_reason"] = "；".join(skip_reasons)
    return case


def generate(target: str, level: str = "L1") -> list[dict[str, Any]]:
    """產生 target transition 的用例：L0 happy（+L1 時每個 branch 一條負向）。"""
    u = unit_spec(target)
    cases: list[dict[str, Any]] = [{
        "id": f"gen-{target}-happy",
        "description": f"[L0] {u.get('summary', target)} happy path",
        "vars": {"job_recruit_count": 1},
        "path": _prefix(target) + [_happy_step(target)],
    }]
    if level in ("L1", "L2"):
        for i, br in enumerate(u.get("branches") or []):
            cases.append(_branch_case(target, br, i))
    return cases


def main(argv: list[str] | None = None) -> int:
    import argparse

    import yaml

    ap = argparse.ArgumentParser(prog="worky-case-gen")
    ap.add_argument("target", help="目標 transition，如 J2_labor_apply")
    ap.add_argument("--level", default="L1", choices=["L0", "L1", "L2"])
    ap.add_argument("--write", action="store_true", help="寫入 cases/generated/")
    args = ap.parse_args(argv)

    cases = generate(args.target, args.level)
    runnable = [c for c in cases if not c.get("skip")]
    skipped = [c for c in cases if c.get("skip")]
    print(f"產生 {len(cases)} 條（可跑 {len(runnable)}，skip {len(skipped)}）：")
    for c in cases:
        tag = f"SKIP（{c['skip_reason']}）" if c.get("skip") else f"{len(c['path'])} 步"
        print(f"  - {c['id']}  [{tag}]")
    if args.write:
        GENERATED.mkdir(parents=True, exist_ok=True)
        for c in cases:
            (GENERATED / f"{c['id']}.yaml").write_text(
                yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"↳ 已寫入 {GENERATED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
