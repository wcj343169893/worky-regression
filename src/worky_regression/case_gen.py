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

# caps_lacking → 對應的 deficiency actor 名（_actors_for 盡力提供）。
# 僅列「帳號池可靠提供（只缺該能力）」的；其餘 caps_lacking 分支仍標 skip。
_DEFICIENCY_ACTOR = {
    "verified": "labor_lacking_verified",
    "profile_complete": "labor_lacking_profile_complete",
    "profile_started": "labor_lacking_profile_started",
}


def _guard_skips(name: str) -> list[str]:
    """該 transition guards 裡標記為無法自動化（時間鎖 / 無 API 可替代）的 skip 原因。

    #4 後 endpoints.yaml 不再放 db_exec：時間壓縮 / 打卡碼預寫 / recruit_deadline 等
    改用 satisfy.skip 標記，產生器據此把整支用例標 skip（覆蓋缺口可見、但不可跑）。
    """
    out = []
    for g in (unit_spec(name).get("guards") or []):
        r = (g.get("satisfy") or {}).get("skip")
        if r:
            out.append(r)
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


def _chain_skips(target: str) -> list[str]:
    """target 自身 + 其前序鏈所有 guard 的 skip 原因（去重保序）。

    任一前置守衛無法自動化（時間鎖等）→ 整條經過它的用例都跑不了，故彙整成用例層 skip。
    """
    reasons: list[str] = []
    for name in _prereq_chain(target) + [target]:
        for r in _guard_skips(name):
            if r not in reasons:
                reasons.append(r)
    return reasons


def _prefix(target: str) -> list[dict[str, Any]]:
    """抵達 target「動作前」的前序 transition（#4 後不再注入 db_exec 前置；caps/by 由帳號池/前序處理）。"""
    return [_happy_step(name) for name in _prereq_chain(target)]


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
    action: dict[str, Any] = {"transition": target, "expect": expect}
    # arrange.request：直接「構建對應的缺失/非法資料」覆寫該步 request 觸發業務分支
    # （時薪過低、工時不符、店鋪不存在等都靠這個變成可跑，不再標 skip）。runner 會淺層覆蓋 body。
    req = (br.get("arrange") or {}).get("request")
    if req:
        action["request"] = dict(req)
    return action


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
    # 前置守衛無法自動化（時間鎖等）→ 整支 skip
    skip_reasons += _chain_skips(target)
    # arrange.skip：此負向情境本身無 API 可佈置（recruit_deadline 拉過去、start_at 改過去等）→ skip
    if arrange.get("skip"):
        skip_reasons.append(arrange["skip"])
    # caps_lacking：綁一個「缺該能力」的 deficiency actor（池可提供者）；否則標 skip
    action_bind = None
    for cap in (arrange.get("caps_lacking") or []):
        actor_name = _DEFICIENCY_ACTOR.get(cap)
        if actor_name:
            action_bind = {"labor": actor_name}
        else:
            skip_reasons.append(f"需配缺能力 {cap!r} 的帳號（帳號池暫無只缺此能力者）")
    if arrange.get("note"):
        skip_reasons.append(f"需特殊安排：{arrange['note']}")

    action = _negative_action(target, br)
    if action_bind:
        action["bind"] = action_bind
    steps.append(action)
    case: dict[str, Any] = {
        "id": f"gen-{target}-neg{idx}-{_slug(br.get('when', ''))}",
        "description": f"[L1 negative] {unit_spec(target).get('summary', target)}：{br.get('when', '')}",
        "vars": {"job_recruit_count": 1},
        "path": steps,
    }
    if skip_reasons:
        case["skip"] = True
        case["skip_reason"] = "；".join(dict.fromkeys(skip_reasons))  # 去重保序
    return case


def generate(target: str, level: str = "L1") -> list[dict[str, Any]]:
    """產生 target transition 的用例：L0 happy（+L1 時每個 branch 一條負向）。"""
    u = unit_spec(target)
    happy: dict[str, Any] = {
        "id": f"gen-{target}-happy",
        "description": f"[L0] {u.get('summary', target)} happy path",
        "vars": {"job_recruit_count": 1},
        "path": _prefix(target) + [_happy_step(target)],
    }
    # 前置守衛有時間鎖等無法自動化者 → happy 流本身也跑不了，整支標 skip（覆蓋缺口可見）
    csk = _chain_skips(target)
    if csk:
        happy["skip"] = True
        happy["skip_reason"] = "；".join(csk)
    cases: list[dict[str, Any]] = [happy]
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
