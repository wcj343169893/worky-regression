"""讀 YAML path → 依序執行 transition → 驗證 HTTP + 推播 + 業務 DB 狀態。"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .actor import Actor
from .registry import PUSH_TYPE_IDS, get as get_transition
from .verifier import DBVerifier


VAR_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def _job_slot_vars(now: int) -> dict[str, Any]:
    """為工作發佈算一組唯一的未來時段變數（避開時段衝突）。"""
    # 限制：工作開始日期需在「今天 +14 天」內（MAX_WORK_INTERVAL_DAYS），
    # 且需 > 今天 +約2天（recruit_deadline/min-publish-interval）。取 +3 ~ +13 天。
    hour = 8 + (now // 60) % 9            # 08:00 ~ 16:00，每分鐘輪一格
    day_off = 3 + (now // 1800) % 11      # +3 ~ +13 天，每 30 分鐘輪一天
    base = now + day_off * 86400
    return {
        "job_start_date": int(time.strftime("%Y%m%d", time.localtime(base))),
        "job_start_period": f"{hour:02d}:00",
        "job_end_period": f"{hour + 2:02d}:00",   # 2 小時
        "job_work_minutes": 120,
    }


@dataclass
class PathExecutionState:
    """跑一條 path 過程中累積的 runtime state（保存 task_sn 等）。"""
    vars: dict[str, Any] = field(default_factory=dict)
    actors: dict[str, Actor] = field(default_factory=dict)

    def resolve(self, raw: Any) -> Any:
        """遞迴展開 {{state.task_sn}} / {{publisher.shop_id}} 等變數。"""
        if isinstance(raw, str):
            # 整個字串就是單一 {{...}} → 回傳原型別（int/list 等不被轉成字串），
            # 例如 {{labor.user_id}} 保持 int、{{state.job_start_date}} 保持 int。
            full = VAR_PATTERN.fullmatch(raw.strip())
            if full:
                return self._lookup(full.group(1))
            return VAR_PATTERN.sub(lambda m: str(self._lookup(m.group(1))), raw)
        if isinstance(raw, dict):
            return {k: self.resolve(v) for k, v in raw.items()}
        if isinstance(raw, list):
            return [self.resolve(x) for x in raw]
        return raw

    def _lookup(self, dotted: str) -> Any:
        parts = dotted.split(".")
        head, rest = parts[0], parts[1:]
        if head == "state":
            cur: Any = self.vars
        elif head in self.actors:
            cur = self.actors[head]
        else:
            raise KeyError(f"unknown variable root: {head!r} in {dotted!r}")

        for p in rest:
            if isinstance(cur, dict):
                cur = cur[p]
            else:
                cur = getattr(cur, p)
        return cur


class PathRunner:
    def __init__(self, db: DBVerifier):
        self.db = db

    def run(self, path_file: Path, *, publisher: Actor | None = None,
            receiver: Actor | None = None, employer: Actor | None = None,
            labor: Actor | None = None, actors: dict[str, Actor] | None = None) -> None:
        """執行一條 path。

        承攬制傳 publisher/receiver；工作系統傳 employer/labor。
        也可直接給 actors dict（key = transition 的 actor_role / pushes_to）。
        """
        with path_file.open() as f:
            spec = yaml.safe_load(f)

        state = self.init_state(
            actors=actors, publisher=publisher, receiver=receiver,
            employer=employer, labor=labor,
        )
        for step in spec["path"]:
            if "db_exec" in step:
                self._run_db_exec(step, state)
            else:
                self._run_step(step, state)

    @staticmethod
    def init_state(*, actors: dict[str, Actor] | None = None,
                   publisher: Actor | None = None, receiver: Actor | None = None,
                   employer: Actor | None = None, labor: Actor | None = None,
                   ) -> PathExecutionState:
        """建立一次 path 執行的初始 state（actor map + runtime 變數）。"""
        actor_map: dict[str, Actor] = dict(actors or {})
        for role, act in (("publisher", publisher), ("receiver", receiver),
                          ("employer", employer), ("labor", labor)):
            if act is not None:
                actor_map[role] = act

        now = int(time.time())
        return PathExecutionState(
            actors=actor_map,
            vars={
                "run_id": uuid.uuid4().hex[:8],
                # --- 承攬制任務時段 ---
                # start_time >= now + MIN_PUBLISH_INTERVAL（dev 720s）；end-start >= 3600s
                "start_time": now + 900,           # 15 分鐘後開始
                "end_time": now + 900 + 3700,      # +1h1m
                # --- 工作系統發佈用 ---
                # 每次跑用「不同的未來時段」，避免打工夥伴在同一時段重複被確認工作
                # （30207「該時段已有確認工作」）。日期每 30 分鐘輪進、時段每分鐘輪進，
                # 一個 session 內幾乎不會撞號。工時固定 120 分（start→end 差 2h、rest 0）。
                **_job_slot_vars(now),
            },
        )

    def _run_db_exec(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """執行任意 SQL（用於模擬外部副作用，例如 ATM 付款完成）。

        Worky model 層有 memcached cache，UPDATE 後預設會 flush_all 失效快取。
        若 sql 是 'SELECT 1' 之類的 no-op，僅執行 cache flush。
        """
        sql = state.resolve(step["db_exec"])
        affected = self.db.execute(sql)
        # SELECT 不需 affected_rows 檢查
        if not sql.strip().upper().startswith("SELECT"):
            expected_min = step.get("expect_min_affected", 1)
            if affected < expected_min:
                raise AssertionError(
                    f"[db_exec] expected >= {expected_min} affected rows, got {affected}\n"
                    f"sql: {sql}"
                )
        flushed = self.db.flush_memcached() if step.get("flush_cache", True) else False
        print(f"  [db_exec] {sql[:60]}... → affected={affected} cache_flushed={flushed}")
        return {"sql": sql, "affected": affected, "cache_flushed": flushed}

    def _run_step(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        transition = get_transition(step["transition"])
        actor = state.actors[transition.actor_role]

        body = state.resolve(transition.body_template)

        watermark = self.db.max_notification_id()
        resp = actor.client.request(transition.method, transition.endpoint, body=body)

        expected_http = step.get("expect", {}).get("http", 200)
        if resp.status_code != expected_http:
            raise AssertionError(
                f"[{transition.name}] HTTP {resp.status_code} != expected {expected_http}; "
                f"body={resp.text[:500]}"
            )

        # 業務層成功檢查：worky 統一回 {success, code, data}；success=false 必失敗
        payload: dict = {}
        if resp.headers.get("content-type", "").startswith("application/json"):
            payload = resp.json()
            if payload.get("success") is False:
                raise AssertionError(
                    f"[{transition.name}] API success=false: "
                    f"code={payload.get('code')} message={payload.get('message')!r} "
                    f"data={payload.get('data')}"
                )

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        obs: dict[str, Any] = {
            "transition": transition.name,
            "endpoint": transition.endpoint,
            "http": resp.status_code,
            "code": payload.get("code"),
            "saved": {},
            "checks": [],
        }

        # 保存 response 中的欄位（例如 task_sn）到 state.vars
        save = step.get("save") or {}
        for var_name, json_path in save.items():
            val = self._dig(data, json_path)
            state.vars[var_name] = val
            obs["saved"][var_name] = val

        # 推播驗證：只要 step.expect 有 push 鍵（即使空）就驗證該類型推播是否落地；
        # 空 push 只驗 type_id，帶 title_contains/body_contains 才額外比對文字。
        expect = step.get("expect", {})
        if "push" in expect and transition.pushes_to:
            push_expect = expect["push"] or {}
            target_actor = state.actors[transition.pushes_to]
            type_id = PUSH_TYPE_IDS[transition.push_type_id]

            push = self.db.assert_push(
                watermark,
                recipient_uid=target_actor.user_id,
                recipient_user_type=target_actor.user_type,
                expected_type_id=type_id,
                title_contains=state.resolve(push_expect.get("title_contains")) if push_expect.get("title_contains") else None,
                body_contains=state.resolve(push_expect.get("body_contains")) if push_expect.get("body_contains") else None,
            )
            obs["checks"].append({"kind": "push", "type_id": type_id,
                                  "to": transition.pushes_to, "push_id": push.id})

        # 業務狀態驗證（query DB）
        state_expect = step.get("expect", {}).get("state")
        if state_expect:
            sql = state.resolve(state_expect["sql"])
            row = self.db.query_one(sql)
            if row is None:
                raise AssertionError(f"[{transition.name}] state query returned no rows: {sql}")
            for col, expected in state_expect.get("equals", {}).items():
                expected = state.resolve(expected)
                actual = row.get(col)
                if str(actual) != str(expected):
                    raise AssertionError(
                        f"[{transition.name}] state.{col}: expected {expected!r}, got {actual!r}"
                    )
                obs["checks"].append({"kind": "state", "col": col, "value": actual})

        return obs

    @staticmethod
    def _dig(data: dict, dotted: str) -> Any:
        cur: Any = data
        for p in dotted.split("."):
            if isinstance(cur, list) and p.isdigit():
                cur = cur[int(p)]
            else:
                cur = cur[p]
        return cur
