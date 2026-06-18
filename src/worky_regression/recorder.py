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
from typing import Any, Callable

import yaml

from .actor import Actor
from .qa_store import QAStore, make_run_id
from .runner import PathRunner, StepAPIError, SuspendRun

# 撞到這些業務錯誤碼時，若有 actor_swapper 就自動換號重試本步（不換 employer，工作已發佈）：
#   30229 LABOR_THAT_DAY_MATCH_JOB_SUCCESS_ONLINE — 同一企業每日僅限工作一次
#   30213 LABOR_HAS_JOB_ON_THE_SAME_DAY_AT_THE_SHOP — 同一商家每日僅限工作一次
# 兩者都是「(夥伴×商家×日) 配對燒掉」，換一個夥伴即可繞開；配發層的配對史避撞對
# 框架外的手動操作/史料遺失的舊 run 是盲的，這裡是後端最終裁決暴露後的兜底。
SWAP_RETRY_CODES = {30229, 30213}
MAX_ACTOR_SWAPS_PER_STEP = 2   # 同一步最多自動換號重試次數（防池小/同因失敗時打轉）


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
    # 參與本次執行的帳號快照：{role: {phone, user_id, user_type, shop_id, display_name}}
    actors: dict[str, dict[str, Any]] | None = None

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

    def __init__(self, db, *, qa_store: QAStore | None = None, system: str = "",
                 actor_swapper: Callable[[str, Actor, Any], Actor | None] | None = None):
        """actor_swapper（可選）：步驟撞 SWAP_RETRY_CODES 時的自動換號回呼。

        簽名 ``(actor_name, old_actor, state) -> Actor | None``：回新 Actor 就把
        state.actors 裡所有指向 old_actor 的別名（labor/labor1 同人）換掉並重試本步；
        回 None / 拋例外則照常記失敗。實作見 autotest.job_actor_swapper。
        """
        self.runner = PathRunner(db)
        self.qa_store = qa_store
        self.system = system
        self.actor_swapper = actor_swapper

    def run(self, path: str | Path | dict, *,
            publisher: Actor | None = None, receiver: Actor | None = None,
            employer: Actor | None = None, labor: Actor | None = None,
            actors: dict[str, Actor] | None = None,
            write: bool = True, started_at: int | None = None,
            resume: dict | None = None,
            on_event: Callable[[str, dict], None] | None = None) -> RunResult:
        """逐步執行並落地。

        on_event（可選）：每步開始/結束、整支開始/結束都回呼一次，供看板做 SSE 即時逐步刷新。
          事件型別與 payload：
            run_start  {run_id, total, transitions:[...]}
            step_start {index, kind, name, tindex|None}
            step_end   {index, status, elapsed_ms, error, tindex|None}
            run_end    {status, failed_at, passed, total}
          tindex 是「只數 transition 步驟」的序號（跳過 db_exec/sleep/assert_*），
          與看板 chip 一一對應；非 transition 步驟為 None。
          回呼自身的例外（含前端關頁造成的 BrokenPipeError）一律吞掉——
          推送失敗不能中斷執行，整支仍須跑完並照常落地。
        """
        spec = path if isinstance(path, dict) else self._load(path)
        # run_id 前移：run_start 事件需要它，故 id 校驗 + 計算都在進迴圈前完成
        path_id = spec.get("id")
        if not path_id:
            raise ValueError("用例缺少 id：每筆用例都必須有唯一 id 才能落庫追溯（請在 YAML 補 id:）")
        # resume（resume_worker 喚醒掛起的 run）：沿用原 run_id / started_at，從 checkpoint
        # 完整還原 state.vars（job_sn / 打卡碼 / job_start_at 等冷凍當下的值，不重算時段），
        # actors 由呼叫端依快照重新登入後帶入。start_index = 要續跑的步序（掛起的 wait_until）。
        resuming = resume is not None
        if resuming:
            run_id = resume["run_id"]
            ts = int(resume.get("started_at") or 0)
        else:
            ts = started_at if started_at is not None else int(time.time())
            run_id = make_run_id(path_id, ts)

        state = self.runner.init_state(
            actors=actors, publisher=publisher, receiver=receiver,
            employer=employer, labor=labor, extra_vars=spec.get("vars"),
        )
        if resuming:
            # 全量覆蓋為冷凍當下的 vars（含已 save 的 job_sn/start_code 等），確定性還原
            state.vars = dict(resume.get("vars") or {})

        def emit(etype: str, payload: dict) -> None:
            if on_event is None:
                return
            try:
                on_event(etype, payload)
            except Exception:  # noqa: BLE001 — 推送失敗（含 BrokenPipeError）不可中斷執行
                pass

        # skip 用例（時間鎖 / 無 API 可替代）：不執行任何步驟、不打被測 API，
        # 只記一筆全 skipped 的 run。stopped 從一開始就 True，迴圈會把每步記成 skipped。
        skipped = bool(spec.get("skip"))
        skip_reason = str(spec.get("skip_reason", "")).strip()

        # resume 時把先前已跑的步驟（0..start_index-1）seed 回 steps，讓收尾 _persist
        # （冪等刪後重插）寫出「完整」step 集合，而非只剩這次續跑的尾段。
        start_index = int(resume.get("resume_step_index") or 0) if resuming else 0
        steps: list[StepResult] = []
        if resuming and self.qa_store is not None:
            steps = [StepResult(**s) for s in self.qa_store.load_run_steps(run_id)]
        stopped = skipped

        emit("run_start", {"run_id": run_id, "started_at": ts, "total": len(spec["path"]),
                           "skipped": skipped, "skip_reason": skip_reason, "resuming": resuming,
                           "start_index": start_index,
                           "transitions": [s.get("transition") for s in spec["path"]
                                           if s.get("transition")]})

        desc = str(spec.get("description", "")).strip()
        # 逐步落庫（崩潰留痕）：開頭先落 status='running'，每步結束即落一筆。
        # 進程死掉時 QA 庫至少留有「跑到第幾步」；殘留的 running 列由看板啟動時收斂成
        # interrupted。任何落庫失敗降級為「跑完一次性落地」，不可中斷執行；
        # 正常結束仍由 _persist（冪等刪後重插）收尾，順帶修復漏寫。
        live = write and self.qa_store is not None
        # resume 不重開 run 列（begin_run 會 DELETE 既有 steps）：續跑直接 append 尾段，
        # 並把 status 從 'resuming' 翻回 'running'（同批帳號已重租、開始實跑）。
        if live and resuming:
            try:
                self.qa_store.mark_run_running(run_id)
            except Exception as e:  # noqa: BLE001
                print(f"[recorder] resume 標 running 失敗（不致命）：{e}")
        elif live:
            try:
                self.qa_store.begin_run(
                    run_id=run_id, case_id=path_id, system=self.system,
                    description=desc, started_at=ts, total=len(spec["path"]),
                    actors=self._snapshot_actors(state))
            except Exception as e:  # noqa: BLE001
                live = False
                print(f"[recorder] 逐步落庫失敗，降級為跑完一次性落地：{e}")

        def live_step(sr: StepResult) -> None:
            nonlocal live
            if not live:
                return
            try:
                self.qa_store.append_step(run_id, asdict(sr))
            except Exception as e:  # noqa: BLE001
                live = False
                print(f"[recorder] 逐步落庫失敗，降級為跑完一次性落地：{e}")

        try:
            self._run_steps(spec, state, steps, emit, live_step, stopped, start_index=start_index)
        except SuspendRun as e:
            # 長延時掛起：不是失敗——冷凍這次執行交給 resume_worker。已跑步驟(0..N-1)已逐步
            # 落庫；這裡只把 run 標 waiting + 落 checkpoint（resume_at / resume_step_index / 全量
            # state.vars + actor 快照）。同一支用例可多次掛起/喚醒（先等開工、再等近結束）。
            resume_idx = e.step_index if e.step_index is not None else len(steps)
            checkpoint = {
                "vars": state.vars,
                "actors": self._snapshot_actors(state),
                "system": self.system,
                "case_id": path_id,
                "description": desc,
                "started_at": ts,
            }
            if write and self.qa_store is not None:
                self.qa_store.suspend_run(
                    run_id=run_id, resume_at=e.resume_at,
                    resume_step_index=resume_idx, checkpoint=checkpoint)
            emit("run_suspend", {"resume_at": e.resume_at, "resume_step_index": resume_idx,
                                 "reason": e.reason})
            return RunResult(
                path_id=path_id, description=desc, started_at=ts, status="waiting",
                steps=steps, failed_at=None, run_id=run_id,
                actors=self._snapshot_actors(state))
        except BaseException:
            # 進程級中斷（Ctrl-C / SystemExit）：把已跑的部分收尾成 interrupted 再拋出。
            # kill -9 連這裡都到不了——靠 begin_run 留下的 running 列 + 看板啟動收斂。
            if write and self.qa_store is not None:
                try:
                    self._persist(RunResult(
                        path_id=path_id, description=desc, started_at=ts,
                        status="interrupted", steps=steps,
                        failed_at=next((s.index for s in steps if s.status == "failed"), None),
                        run_id=run_id, actors=self._snapshot_actors(state)))
                except Exception:  # noqa: BLE001 — 收尾失敗不可吞掉原始中斷
                    pass
            raise
        failed_at = next((s.index for s in steps if s.status == "failed"), None)
        status = "failed" if failed_at is not None else "passed"

        if skipped:
            status = "skipped"   # 全程未執行，狀態獨立標記（前端顯示「略過」而非「失敗」）
        result = RunResult(
            path_id=path_id,
            description=desc,
            started_at=ts,
            status=status,
            steps=steps,
            failed_at=failed_at,
            run_id=run_id,
            actors=self._snapshot_actors(state),
        )
        if write and self.qa_store is not None:
            self._persist(result)
        emit("run_end", {"status": status, "failed_at": failed_at,
                        "skipped": skipped, "skip_reason": skip_reason,
                        "passed": sum(1 for s in steps if s.status == "passed"),
                        "total": len(steps)})
        return result

    def _run_steps(self, spec: dict, state, steps: list[StepResult],
                   emit: Callable[[str, dict], None],
                   live_step: Callable[[StepResult], None], stopped: bool,
                   start_index: int = 0) -> None:
        """逐步執行主迴圈：結果 append 進 steps 並即時落庫（live_step）。

        start_index>0（resume 續跑）：i < start_index 的步驟先前已跑過、已 seed 進 steps，
        這裡只快轉（仍推進 tindex 讓 chip 序號對齊），不重跑、不重記。
        """
        tindex = -1  # transition 序號（與 chip 對應）；每遇 transition 步驟 +1
        for i, step in enumerate(spec["path"]):
            is_db = "db_exec" in step
            is_sleep = "sleep" in step
            is_assert = "assert_state" in step
            is_assert_api = "assert_api" in step
            is_wait_api = "wait_api" in step
            is_wait_until = "wait_until" in step
            if is_db:
                kind = name = "db_exec"
            elif is_assert:
                kind, name = "assert_state", "assert_state"
            elif is_assert_api:
                kind, name = "assert_api", "assert_api"
            elif is_wait_api:
                kind, name = "wait_api", f"wait_api {step['wait_api'].get('query', '')}"
            elif is_wait_until:
                kind, name = "wait_until", f"wait_until {step['wait_until'].get('anchor', step['wait_until'].get('at',''))}"
            elif is_sleep:
                kind, name = "sleep", f"sleep {step['sleep']}s"
            else:
                kind, name = "transition", step.get("transition", "?")
            ti = (tindex := tindex + 1) if kind == "transition" else None
            if i < start_index:
                continue   # resume：先前已跑過的步驟，只推進 tindex（上一行已做）
            if stopped:
                steps.append(StepResult(i, kind, name, "skipped", 0))
                live_step(steps[-1])
                emit("step_end", {"index": i, "status": "skipped",
                                  "elapsed_ms": 0, "error": None, "tindex": ti})
                continue
            # 等待類步驟帶時長供前端倒計時：sleep 是精確秒數；wait_api 是逾時上限
            # （條件滿足會提前結束），預設值與 runner._run_wait_api 一致。
            # next_tindex = 下一個 transition 的 chip 序號——前端把「等待中 + 倒數」直接
            # 疊在那顆 chip 上（如 J6 等待中 14:22）；等待在最後一個 transition 之後時
            # 沒有下一顆，前端退回尾部掛暫時 chip。
            wait_secs = None
            if is_wait_api:
                wait_secs = float(step["wait_api"].get("timeout", 30))
            elif is_wait_until:
                wait_secs = None   # 倒數由 anchor 推算，前端不預知（可能 inline 也可能掛起）
            elif is_sleep:
                wait_secs = float(step["sleep"])
            emit("step_start", {"index": i, "kind": kind, "name": name, "tindex": ti,
                                "wait_secs": wait_secs,
                                "next_tindex": (None if ti is not None else tindex + 1)})
            t0 = time.time()
            swaps = 0   # 本步已自動換號重試的次數（耗時累計入同一步，不另記一筆）
            while True:
                try:
                    if is_db:
                        obs = self.runner._run_db_exec(step, state)
                    elif is_assert:
                        obs = self.runner._run_assert(step, state)
                    elif is_assert_api:
                        obs = self.runner._run_assert_api(step, state)
                    elif is_wait_api:
                        obs = self.runner._run_wait_api(step, state)
                    elif is_wait_until:
                        obs = self.runner._run_wait_until(step, state)
                    elif is_sleep:
                        obs = self.runner._run_sleep(step, state)
                    else:
                        obs = self.runner._run_step(step, state)
                    elapsed = int((time.time() - t0) * 1000)
                    if swaps:
                        obs = obs or {}
                        obs["actor_swaps"] = swaps
                    steps.append(StepResult(i, kind, name, "passed", elapsed, obs or {}))
                    live_step(steps[-1])
                    emit("step_end", {"index": i, "status": "passed",
                                      "elapsed_ms": elapsed, "error": None, "tindex": ti})
                except SuspendRun as e:
                    # 長延時掛起：不換號、不記失敗，帶上「續跑步序」往外傳給 run() 冷凍。
                    e.step_index = i
                    raise
                except Exception as e:  # noqa: BLE001 — 記錄任何失敗，含 AssertionError
                    if (kind == "transition" and swaps < MAX_ACTOR_SWAPS_PER_STEP
                            and self._swap_actor(e, state)):
                        swaps += 1
                        continue
                    elapsed = int((time.time() - t0) * 1000)
                    err = f"{type(e).__name__}: {e}"
                    if swaps:
                        err = f"{err}（已自動換號重試 {swaps} 次）"
                    steps.append(StepResult(i, kind, name, "failed", elapsed, error=err))
                    live_step(steps[-1])
                    stopped = True
                    emit("step_end", {"index": i, "status": "failed",
                                      "elapsed_ms": elapsed, "error": err, "tindex": ti})
                break

    def _swap_actor(self, exc: Exception, state) -> bool:
        """步驟失敗的自動換號：撞 SWAP_RETRY_CODES 且能從池配到替補就換掉重試。

        只認 StepAPIError（帶 code/actor_name 的正向失敗）；負向斷言（expect.success=false）
        錯誤碼不符等其他失敗是裸 AssertionError，不會誤觸換號。換號把 state.actors 裡
        所有指向同一人的別名（labor 與 labor1 同人）一起替換，後續步驟與 push 驗證才一致。
        """
        if self.actor_swapper is None or not isinstance(exc, StepAPIError):
            return False
        if exc.code not in SWAP_RETRY_CODES or not exc.actor_name:
            return False
        old = state.actors.get(exc.actor_name)
        if old is None:
            return False
        try:
            new = self.actor_swapper(exc.actor_name, old, state)
        except Exception as e:  # noqa: BLE001 — 換號失敗不擋原始失敗的落地
            print(f"  [swap] 自動換號失敗（{exc.actor_name}, code={exc.code}）：{e}")
            return False
        if new is None:
            return False
        for k, v in list(state.actors.items()):
            if v is old:
                state.actors[k] = new
        print(f"  [swap] code={exc.code}：{exc.actor_name} "
              f"#{getattr(old, 'user_id', '?')} → #{getattr(new, 'user_id', '?')}，重試本步")
        return True

    @staticmethod
    def _snapshot_actors(state) -> dict[str, dict[str, Any]]:
        """擷取本次執行的帳號身份（不含 client 等不可序列化欄位），供詳情頁展示參與帳號。"""
        snap: dict[str, dict[str, Any]] = {}
        for role, a in (getattr(state, "actors", None) or {}).items():
            snap[role] = {
                "phone": getattr(a, "phone", None),
                "user_id": getattr(a, "user_id", None),
                "user_type": getattr(a, "user_type", None),
                "shop_id": getattr(a, "shop_id", None),
                "display_name": getattr(a, "display_name", ""),
            }
        return snap

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
            actors=result.actors,
        )

    @staticmethod
    def _load(path: str | Path) -> dict:
        with Path(path).open(encoding="utf-8") as f:
            return yaml.safe_load(f)
