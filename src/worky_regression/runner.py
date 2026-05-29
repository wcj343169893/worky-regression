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
from .transitions import Transition, get as get_transition
from .verifier import DBVerifier


VAR_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


@dataclass
class PathExecutionState:
    """跑一條 path 過程中累積的 runtime state（保存 task_sn 等）。"""
    vars: dict[str, Any] = field(default_factory=dict)
    actors: dict[str, Actor] = field(default_factory=dict)

    def resolve(self, raw: Any) -> Any:
        """遞迴展開 {{state.task_sn}} / {{publisher.shop_id}} 等變數。"""
        if isinstance(raw, str):
            def repl(m: re.Match) -> str:
                key = m.group(1)
                return str(self._lookup(key))
            return VAR_PATTERN.sub(repl, raw)
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

    def run(self, path_file: Path, *, publisher: Actor, receiver: Actor) -> None:
        with path_file.open() as f:
            spec = yaml.safe_load(f)

        now = int(time.time())
        state = PathExecutionState(
            actors={"publisher": publisher, "receiver": receiver},
            vars={
                "run_id": uuid.uuid4().hex[:8],
                # 任務時段條件：
                # - start_time >= now + MIN_PUBLISH_INTERVAL_SECONDS（dev 配 720s ≈ 0.2h）
                # - end_time - start_time >= 3600s
                # taskStart/taskEnd service 內不檢查實際時間，只 stamp now，所以
                # test 連續跑沒問題；只要 publish 一刻通過驗證即可
                "start_time": now + 900,           # 15 分鐘後開始
                "end_time": now + 900 + 3700,      # +1h1m
            },
        )

        for step in spec["path"]:
            if "db_exec" in step:
                self._run_db_exec(step, state)
            else:
                self._run_step(step, state)

    def _run_db_exec(self, step: dict, state: PathExecutionState) -> None:
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

    def _run_step(self, step: dict, state: PathExecutionState) -> None:
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

        # 保存 response 中的欄位（例如 task_sn）到 state.vars
        save = step.get("save") or {}
        for var_name, json_path in save.items():
            state.vars[var_name] = self._dig(data, json_path)

        # 推播驗證
        push_expect = step.get("expect", {}).get("push")
        if push_expect and transition.pushes_to:
            target_actor = state.actors[transition.pushes_to]
            from .push_type_ids import PUSH_TYPE_IDS  # lazy import
            type_id = PUSH_TYPE_IDS[transition.push_type_id]

            push = self.db.assert_push(
                watermark,
                recipient_uid=target_actor.user_id,
                recipient_user_type=target_actor.user_type,
                expected_type_id=type_id,
                title_contains=state.resolve(push_expect.get("title_contains")) if push_expect.get("title_contains") else None,
                body_contains=state.resolve(push_expect.get("body_contains")) if push_expect.get("body_contains") else None,
            )

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

    @staticmethod
    def _dig(data: dict, dotted: str) -> Any:
        cur: Any = data
        for p in dotted.split("."):
            if isinstance(cur, list) and p.isdigit():
                cur = cur[int(p)]
            else:
                cur = cur[p]
        return cur
