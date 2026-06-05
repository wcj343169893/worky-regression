"""Layer ④ — 執行結果記錄器。

``PathRunner.run`` 是「跑到第一個失敗就 raise」的嚴格模式（給 pytest 用）。
本模組的 ``RecordingRunner`` 改為**逐步記錄**：每一步收集 http/code/push/state 觀測值
與耗時，遇到失敗則記下錯誤並停止（狀態機不能跳關），最後把整條 path 的結果落地成 JSON。

這是 AI 分解器（Layer ③）跑完任務流後「記錄結果」的承接點，也可單獨手動使用：

    from worky_regression.recorder import RecordingRunner
    res = RecordingRunner(db).run("cases/job-happy-core.yaml", employer=emp, labor=lab)
    print(res.status, res.failed_at)
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .actor import Actor
from .qa_store import QAStore, make_run_id
from .runner import PathRunner


@dataclass
class StepResult:
    index: int
    kind: str                       # "transition" | "db_exec"
    name: str                       # transition 名稱或 "db_exec"
    status: str                     # "passed" | "failed" | "skipped"
    elapsed_ms: int
    observations: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunResult:
    path_id: str
    description: str
    started_at: int                 # unix 秒
    status: str                     # "passed" | "failed"
    steps: list[StepResult]
    failed_at: int | None = None    # 失敗步的 index
    run_id: str | None = None       # 每次執行唯一 id（{path_id}-{started_at}-{hex}）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def summary(self) -> str:
        passed = sum(1 for s in self.steps if s.status == "passed")
        tail = "" if self.status == "passed" else \
            f"；失敗於 step[{self.failed_at}] {self.steps[self.failed_at].name}"
        return f"{self.path_id}: {self.status}（{passed}/{len(self.steps)} 步通過）{tail}"


class RecordingRunner:
    """跑一條 path 並逐步記錄結果，不在失敗時 raise；結果落地到 worky_qa_dashboard。"""

    def __init__(self, db, *, qa_store: QAStore | None = None, system: str = ""):
        self.runner = PathRunner(db)
        self.qa_store = qa_store
        self.system = system

    def run(self, path: str | Path | dict, *,
            publisher: Actor | None = None, receiver: Actor | None = None,
            employer: Actor | None = None, labor: Actor | None = None,
            actors: dict[str, Actor] | None = None,
            write: bool = True, started_at: int | None = None) -> RunResult:
        spec = path if isinstance(path, dict) else self._load(path)
        state = self.runner.init_state(
            actors=actors, publisher=publisher, receiver=receiver,
            employer=employer, labor=labor, extra_vars=spec.get("vars"),
        )

        steps: list[StepResult] = []
        status = "passed"
        failed_at: int | None = None
        stopped = False

        for i, step in enumerate(spec["path"]):
            is_db = "db_exec" in step
            is_sleep = "sleep" in step
            is_assert = "assert_state" in step
            is_assert_api = "assert_api" in step
            if is_db:
                kind = name = "db_exec"
            elif is_assert:
                kind, name = "assert_state", "assert_state"
            elif is_assert_api:
                kind, name = "assert_api", "assert_api"
            elif is_sleep:
                kind, name = "sleep", f"sleep {step['sleep']}s"
            else:
                kind, name = "transition", step.get("transition", "?")
            if stopped:
                steps.append(StepResult(i, kind, name, "skipped", 0))
                continue
            t0 = time.time()
            try:
                if is_db:
                    obs = self.runner._run_db_exec(step, state)
                elif is_assert:
                    obs = self.runner._run_assert(step, state)
                elif is_assert_api:
                    obs = self.runner._run_assert_api(step, state)
                elif is_sleep:
                    obs = self.runner._run_sleep(step, state)
                else:
                    obs = self.runner._run_step(step, state)
                steps.append(StepResult(i, kind, name, "passed",
                                        int((time.time() - t0) * 1000), obs or {}))
            except Exception as e:  # noqa: BLE001 — 記錄任何失敗，含 AssertionError
                steps.append(StepResult(i, kind, name, "failed",
                                        int((time.time() - t0) * 1000),
                                        error=f"{type(e).__name__}: {e}"))
                status, failed_at, stopped = "failed", i, True

        path_id = spec.get("id")
        if not path_id:
            raise ValueError("用例缺少 id：每筆用例都必須有唯一 id 才能落庫追溯（請在 YAML 補 id:）")
        ts = started_at if started_at is not None else int(time.time())
        result = RunResult(
            path_id=path_id,
            description=str(spec.get("description", "")).strip(),
            started_at=ts,
            status=status,
            steps=steps,
            failed_at=failed_at,
            run_id=make_run_id(path_id, ts),
        )
        if write and self.qa_store is not None:
            self._persist(result)
        return result

    def _persist(self, result: RunResult) -> None:
        self.qa_store.insert_run(
            run_id=result.run_id,
            case_id=result.path_id,
            system=self.system,
            status=result.status,
            description=result.description,
            started_at=result.started_at,
            failed_at=result.failed_at,
            steps=[asdict(s) for s in result.steps],
            source="run",
        )

    @staticmethod
    def _load(path: str | Path) -> dict:
        with Path(path).open(encoding="utf-8") as f:
            return yaml.safe_load(f)
