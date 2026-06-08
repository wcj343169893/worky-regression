"""讀 YAML path → 依序執行 transition → 驗證 HTTP + 推播 + 業務 DB 狀態。"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .actor import Actor
from .registry import PUSH_TYPE_IDS, get as get_transition, query_unit
from .verifier import DBVerifier, PushAssertionError


VAR_PATTERN = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class DBAccessDisabled(RuntimeError):
    """執行期一律不碰被測 DB（#4）：db_exec / expect.state / assert_state 已停用。

    狀態查驗改打查詢端點（expect.api / assert_api / wait_api）；需等後端定時任務/隊列
    推進狀態時用 wait_api 輪詢。時間鎖類用例（打卡 / contract 開始結束）無 API 可替代，標 skip。
    """


# staging 的 min_publish_interval_seconds / recruit_deadline_offset_seconds 約 600s，
# 即工作開始時間只需比現在晚約 10 分鐘。這裡 buffer 取 900s（含請求延遲與 Python/PHP 時鐘飄移裕度）：
# 「今天該時刻」距現在不足這個值就順延隔天，避免被後端 START_AT_IS_LESS_THAN_LIMIT 擋下。
_TODAY_SLOT_LEAD_BUFFER = 900


def _anchor_today_slot(now: int, hhmm: str, work_minutes: int) -> dict[str, Any]:
    """把工作時段錨定在「今天 hhmm」；今天該時刻距現在不足 buffer 秒則順延隔天 hhmm。

    用例以 ``vars: {job_start_time_of_day: "15:30"}`` opt-in；回傳會覆寫 _job_slot_vars
    的 job_start_date / job_start_period / job_end_period / job_work_minutes。
    """
    h, m = (int(x) for x in hhmm.split(":"))
    lt = time.localtime(now)
    start = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1)))
    if start - now < _TODAY_SLOT_LEAD_BUFFER:
        start += 86400                       # 來不及發今天 → 順延隔天同一時刻
    end_total = h * 60 + m + work_minutes
    return {
        "job_start_date": int(time.strftime("%Y%m%d", time.localtime(start))),
        "job_start_period": f"{h:02d}:{m:02d}",
        "job_end_period": f"{(end_total // 60) % 24:02d}:{end_total % 60:02d}",
        "job_work_minutes": work_minutes,
    }


def _relative_slot(now: int, after_minutes: int, work_minutes: int) -> dict[str, Any]:
    """把工作開始時間錨定在「現在 + after_minutes 分鐘」（相對偏移，永遠在未來）。

    用例以 ``vars: {job_start_after_minutes: 60}`` opt-in（例：發一則 1 小時後的工作）。
    後端由 start_date + start_time_period(HH:MM) 推 start_at（分鐘精度），end 由 work_minutes 推；
    跨午夜由 localtime 自然處理。staging min_publish_interval≈600s，偏移 ≥ ~11 分即可發佈。
    """
    start = now + after_minutes * 60
    end = start + work_minutes * 60
    return {
        "job_start_date": int(time.strftime("%Y%m%d", time.localtime(start))),
        "job_start_period": time.strftime("%H:%M", time.localtime(start)),
        "job_end_period": time.strftime("%H:%M", time.localtime(end)),
        "job_work_minutes": work_minutes,
    }


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
            employer=employer, labor=labor, extra_vars=spec.get("vars"),
        )
        for step in spec["path"]:
            if "db_exec" in step:
                self._run_db_exec(step, state)
            elif "assert_state" in step:
                self._run_assert(step, state)
            elif "assert_api" in step:
                self._run_assert_api(step, state)
            elif "wait_api" in step:
                self._run_wait_api(step, state)
            elif "sleep" in step:
                self._run_sleep(step, state)
            else:
                self._run_step(step, state)

    @staticmethod
    def init_state(*, actors: dict[str, Actor] | None = None,
                   publisher: Actor | None = None, receiver: Actor | None = None,
                   employer: Actor | None = None, labor: Actor | None = None,
                   extra_vars: dict[str, Any] | None = None,
                   ) -> PathExecutionState:
        """建立一次 path 執行的初始 state（actor map + runtime 變數）。

        extra_vars：spec 頂層 ``vars:`` 的覆寫（例如 job_recruit_count），最後合併蓋過預設。
        """
        actor_map: dict[str, Actor] = dict(actors or {})
        for role, act in (("publisher", publisher), ("receiver", receiver),
                          ("employer", employer), ("labor", labor)):
            if act is not None:
                actor_map[role] = act

        now = int(time.time())
        state = PathExecutionState(
            actors=actor_map,
            vars={
                "run_id": uuid.uuid4().hex[:8],
                # --- 承攬制任務時段 ---
                # dev 後端 TaskPublishForm 規則（next-v31x）：
                #   start_time >= now + MIN_PUBLISH_INTERVAL_SECONDS（86400=24h）
                #   且 start_time - RECRUIT_DEADLINE_OFFSET_SECONDS（86400）>= now
                #   且 3600 <= end-start <= 30d、start <= now+90d
                # 取 now + 25h（24h + 1h buffer 防 Python/PHP 間時鐘飄移與請求延遲）。
                # 註：T6/T7「開始/結束任務」需 start_at<=now、end_at>now、pay_status=102，
                #     故發佈後須用 db_exec 把 start_at/end_at 拉回當下（見 path 的橋接步驟）。
                "start_time": now + 90000,         # ≈25 小時後開始（過 24h 門檻）
                "end_time": now + 90000 + 3700,    # +1h1m（end-start 3700s）
                # --- 工作系統發佈用 ---
                # 每次跑用「不同的未來時段」，避免打工夥伴在同一時段重複被確認工作
                # （30207「該時段已有確認工作」）。日期每 30 分鐘輪進、時段每分鐘輪進，
                # 一個 session 內幾乎不會撞號。工時固定 120 分（start→end 差 2h、rest 0）。
                **_job_slot_vars(now),
                # 工作招募人數（J1 用）；多人申請/錄取的用例可在 spec 頂層 vars 覆寫。
                "job_recruit_count": 1,
            },
        )
        if extra_vars:
            state.vars.update(extra_vars)
        # 用例可用兩種 opt-in 覆寫上面 _job_slot_vars 的未來輪轉時段（after_minutes 優先）：
        #   vars: {job_start_after_minutes: 60}   → 現在 + N 分鐘（例：1 小時後的工作）
        #   vars: {job_start_time_of_day: "15:30"} → 今天該時刻（過當日時限自動順延隔天）
        wm = int(state.vars.get("job_work_minutes", 120))
        after = state.vars.get("job_start_after_minutes")
        tod = state.vars.get("job_start_time_of_day")
        if after is not None:
            state.vars.update(_relative_slot(now, int(after), wm))
        elif tod:
            state.vars.update(_anchor_today_slot(now, str(tod), wm))
        return state

    def _run_db_exec(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """已停用（#4）：執行期不碰被測 DB。

        原用途：時間壓縮橋接（start_at/end_at=NOW()）、強制狀態（pay_status=102）、SELECT 查驗。
        遷移指引：查驗改 expect.api / assert_api；等隊列推進狀態用 wait_api；時間鎖類用例標 skip。
        """
        sql = str(step.get("db_exec", ""))[:80]
        raise DBAccessDisabled(
            f"db_exec 已停用（#4 執行期不碰 DB）。改用 transition / assert_api / wait_api，"
            f"時間鎖類用例請標 skip。原 SQL: {sql}…")

    def _run_assert(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """已停用（#4）：assert_state 直接 SELECT 被測 DB。改用 assert_api 打查詢端點比對回應。"""
        raise DBAccessDisabled(
            "assert_state 已停用（#4 執行期不碰 DB）。改用 assert_api（打查詢端點比對回應），"
            "例：{assert_api: {query: Q_labor_job_matches, find: {...}, equals: {...}}}。")

    def _run_sleep(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """暫停數秒。用於繞過後端「執行操作過快」(9002) 類的短窗節流（TTL 約 1s）。"""
        secs = float(step.get("sleep", 0))
        time.sleep(secs)
        print(f"  [sleep] {secs}s")
        return {"slept": secs}

    def _run_step(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        transition = get_transition(step["transition"])

        # 步驟級 role 重綁：bind: {labor: labor2} 讓本步的 request/驗證 SQL/push 目標
        # 一致指向 labor2（多身份用例必要，且完全不用改 endpoints.yaml 模板）。
        bind = step.get("bind")
        if bind:
            actors = dict(state.actors)
            for role, src in bind.items():
                if src not in state.actors:
                    raise KeyError(f"bind: 未知 actor {src!r}（可用：{sorted(state.actors)}）")
                actors[role] = state.actors[src]
            state = replace(state, actors=actors)  # 共用同一 vars dict，save 仍寫回原 state

        actor = state.actors[transition.actor_role]

        body = state.resolve(transition.body_template)

        # 營運活動單元走獨立的 Activity API base（/activity，非主 API /v1）
        base = (actor.client.settings.activity_api_base
                if getattr(transition, "api_group", "main") == "activity" else None)

        # 推播驗證改打 API（#4 不查 DB）：行動前先記下「收件 actor 通知清單水位(max id)」，
        # 行動後查水位之後新出現、type_id 相符的通知。只在本步預期推播時才取水位（省一次查詢）。
        push_watermark = 0
        if "push" in step.get("expect", {}) and transition.pushes_to:
            push_watermark = self._notification_max_id(state.actors[transition.pushes_to])
        # GET 走 query string（params），其餘走 body
        if transition.method.upper() == "GET":
            resp = actor.client.request(transition.method, transition.endpoint,
                                        params=body or None, base=base)
        else:
            resp = actor.client.request(transition.method, transition.endpoint,
                                        body=body, base=base)

        expected_http = step.get("expect", {}).get("http", 200)
        if resp.status_code != expected_http:
            raise AssertionError(
                f"[{transition.name}] HTTP {resp.status_code} != expected {expected_http}; "
                f"body={resp.text[:500]}"
            )

        # 業務層檢查：worky 統一回 {success, code, data}。
        payload: dict = {}
        if resp.headers.get("content-type", "").startswith("application/json"):
            payload = resp.json()
        success = payload.get("success")
        expect = step.get("expect", {})

        # 負向斷言：expect.success=false 表示「預期 API 拒絕」（branches 用）。
        # 此時 success=true 反而是失敗；並可比對 code / message_contains。拒絕了就無副作用，提早返回。
        if expect.get("success") is False:
            if success is not False:
                raise AssertionError(
                    f"[{transition.name}] 預期 API 拒絕(success=false)，實得 success={success!r}"
                    f" code={payload.get('code')} data={payload.get('data')}"
                )
            exp_code = expect.get("code")
            if exp_code is not None and payload.get("code") != exp_code:
                raise AssertionError(
                    f"[{transition.name}] 預期錯誤碼 {exp_code}，實得 {payload.get('code')}"
                    f" message={payload.get('message')!r}"
                )
            sub = state.resolve(expect["message_contains"]) if expect.get("message_contains") else None
            if sub and sub not in (payload.get("message") or ""):
                raise AssertionError(
                    f"[{transition.name}] 預期訊息含 {sub!r}，實得 {payload.get('message')!r}"
                )
            return {
                "transition": transition.name, "endpoint": transition.endpoint,
                "http": resp.status_code, "code": payload.get("code"),
                "message": payload.get("message"), "saved": {},
                "checks": [{"kind": "expect_fail", "code": payload.get("code")}],
            }

        # 正向：success=false 必失敗
        if success is False:
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

            push = self._assert_push_api(
                target_actor, push_watermark, type_id,
                title_contains=state.resolve(push_expect.get("title_contains")) if push_expect.get("title_contains") else None,
                body_contains=state.resolve(push_expect.get("body_contains")) if push_expect.get("body_contains") else None,
            )
            obs["checks"].append({"kind": "push", "type_id": type_id,
                                  "to": transition.pushes_to, "push_id": push.get("id")})

        # 業務狀態驗證（首選）：打對應查詢接口比對回應，驗的是對外行為而非 DB 長相。
        api_expect = expect.get("api")
        if api_expect:
            for one in (api_expect if isinstance(api_expect, list) else [api_expect]):
                obs["checks"].append(self._verify_api(one, state))

        # 業務狀態驗證：expect.state(SQL) 已停用（#4 執行期不碰 DB），改用 expect.api。
        if step.get("expect", {}).get("state"):
            raise DBAccessDisabled(
                f"[{transition.name}] expect.state 已停用（#4 執行期不碰 DB）。"
                f"改用 expect.api（打查詢端點比對回應）：expect: {{api: {{query: ..., equals: {{...}}}}}}。")

        return obs

    # ── 推播驗證改打 API（#4 不查 s_notifications）────────────────────────────
    # 依收件 actor 的 user_type 自動挑通知查詢端點（1→商家、2→打工夥伴；publisher/receiver 皆 2）。
    _NOTIFICATION_QUERY = {1: "Q_employer_notifications", 2: "Q_labor_notifications"}

    def _notification_list(self, actor: Actor) -> list[dict[str, Any]]:
        """以該 actor 身份打通知清單 API，回正規化的通知陣列（每筆含 id/type_id/title/content…）。"""
        qname = self._NOTIFICATION_QUERY.get(actor.user_type)
        if not qname:
            raise PushAssertionError(f"無法為 user_type={actor.user_type} 的 actor 查通知（無對應 query_unit）")
        q = query_unit(qname)
        resp = actor.client.request("GET", q["endpoint"], params=(q.get("request") or {"page": 1}))
        if resp.status_code != 200:
            raise PushAssertionError(f"通知查詢 HTTP {resp.status_code}: {resp.text[:200]}")
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if payload.get("success") is False:
            raise PushAssertionError(
                f"通知查詢失敗 code={payload.get('code')} message={payload.get('message')!r}")
        data = payload.get("data")
        # labor 回 {category_groups, list:[...]}、employer 回 [...]（或同樣帶 list）→ 正規化成陣列
        items = data.get("list") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        return [it for it in (items or []) if isinstance(it, dict)]

    def _notification_max_id(self, actor: Actor) -> int:
        """收件 actor 通知清單目前最大 id（行動前水位）。查不到回 0（視為全部都是新的）。"""
        return max((int(n.get("id") or 0) for n in self._notification_list(actor)), default=0)

    def _assert_push_api(self, actor: Actor, watermark_id: int, expected_type_id: int, *,
                         title_contains: str | None = None, body_contains: str | None = None,
                         timeout_sec: float = 8.0, poll_interval: float = 0.5) -> dict[str, Any]:
        """輪詢收件 actor 的通知清單，直到出現「水位後、type_id 相符」的通知或逾時。

        取代 DBVerifier.assert_push（不再 SELECT s_notifications）。推播可能由隊列非同步落地，
        故輪詢等待（timeout_sec）。命中後再比對 title/content 包含關係。
        """
        deadline = time.time() + timeout_sec
        seen_types: list[int] = []
        while True:
            fresh = [n for n in self._notification_list(actor) if int(n.get("id") or 0) > watermark_id]
            seen_types = [int(n.get("type_id") or 0) for n in fresh]
            matches = [n for n in fresh if int(n.get("type_id") or 0) == expected_type_id]
            if matches:
                push = max(matches, key=lambda n: int(n.get("id") or 0))
                if title_contains and title_contains not in (push.get("title") or ""):
                    raise PushAssertionError(
                        f"推播標題不含 {title_contains!r}，實得 {push.get('title')!r}")
                if body_contains and body_contains not in (push.get("content") or ""):
                    raise PushAssertionError(
                        f"推播內文不含 {body_contains!r}，實得 {push.get('content')!r}")
                print(f"  [push.api] uid={actor.user_id} type_id={expected_type_id} push_id={push.get('id')}")
                return push
            if time.time() >= deadline:
                raise PushAssertionError(
                    f"逾時 {timeout_sec}s：收件 user_id={actor.user_id} 在水位 {watermark_id} 後"
                    f"無 type_id={expected_type_id} 的推播；實得新通知 type_id={seen_types}")
            time.sleep(poll_interval)

    def _verify_api(self, api: dict, state: PathExecutionState) -> dict[str, Any]:
        """打對應查詢接口比對回應欄位（取代 SELECT 驗 DB）。

        api: {query: <query_unit 名>, actor?: <覆寫呼叫者>, args?: {覆寫/補查詢參數},
              http?: 預期狀態碼(預設 200), equals: {回應 data 內欄位路徑: 期望值}}
        """
        q = query_unit(api["query"])
        actor_role = api.get("actor") or q["actor"]
        if actor_role not in state.actors:
            raise KeyError(f"[expect.api {api['query']}] 未知 actor {actor_role!r}（可用：{sorted(state.actors)}）")
        actor = state.actors[actor_role]
        req = {**(q.get("request") or {}), **(api.get("args") or {})}
        body = state.resolve(req)
        base = (actor.client.settings.activity_api_base
                if q.get("api_group", "main") == "activity" else None)
        method = str(q.get("method", "GET")).upper()
        if method == "GET":
            resp = actor.client.request(method, q["endpoint"], params=body or None, base=base)
        else:
            resp = actor.client.request(method, q["endpoint"], body=body, base=base)
        exp_http = api.get("http", 200)
        if resp.status_code != exp_http:
            raise AssertionError(
                f"[expect.api {api['query']}] HTTP {resp.status_code} != {exp_http}; body={resp.text[:300]}")
        payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if payload.get("success") is False:
            raise AssertionError(
                f"[expect.api {api['query']}] 查詢失敗 code={payload.get('code')} message={payload.get('message')!r}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        # find：先在某清單路徑找出符合 where 的單筆，再對它比對 equals（清單型回應用）。
        found_where = None
        find = api.get("find")
        if find:
            lst = self._dig(data, find["in"]) if find.get("in") else data
            if not isinstance(lst, list):
                raise AssertionError(f"[expect.api {api['query']}] find.in={find.get('in')!r} 不是清單")
            where = {k: state.resolve(v) for k, v in (find.get("where") or {}).items()}
            match = next((it for it in lst if all(str(it.get(k)) == str(v) for k, v in where.items())), None)
            if match is None:
                raise AssertionError(
                    f"[expect.api {api['query']}] 清單 {find.get('in')!r} 找不到符合 {where} 的項目（共 {len(lst)} 筆）")
            data, found_where = match, where
        checked: dict[str, Any] = {}
        for path, expected in (api.get("equals") or {}).items():
            expected = state.resolve(expected)
            actual = self._dig(data, path)
            if str(actual) != str(expected):
                raise AssertionError(
                    f"[expect.api {api['query']}] {path}: expected {expected!r}, got {actual!r}")
            checked[path] = actual
        print(f"  [expect.api] {api['query']} {q['endpoint']} as {actor_role}"
              + (f" find={found_where}" if found_where else "") + f" → {checked}")
        return {"kind": "api", "query": api["query"], "endpoint": q["endpoint"],
                "actor": actor_role, "find": found_where, "equals": checked}

    def _run_assert_api(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """獨立的接口斷言步驟（不綁 transition）：直接打查詢接口比對，取代 assert_state(SQL)。
        需在 step 內指定 actor（無 transition 上下文可繼承）。"""
        return self._verify_api(step["assert_api"], state)

    def _run_wait_api(self, step: dict, state: PathExecutionState) -> dict[str, Any]:
        """輪詢查詢端點直到條件成立或逾時——等後端定時任務 / 隊列推進工作/申請狀態用（取代死等 sleep）。

        step: {wait_api: {query, actor?, args?, find?, equals, http?,
                          timeout?(秒,預設30), interval?(秒,預設2)}}
        每輪複用 _verify_api 打一次查詢比對；未滿足(AssertionError)就睡 interval 再試，逾時才失敗。
        """
        spec = step["wait_api"]
        timeout = float(spec.get("timeout", 30))
        interval = float(spec.get("interval", 2))
        deadline = time.time() + timeout
        attempts = 0
        while True:
            attempts += 1
            try:
                res = self._verify_api(spec, state)
                res["kind"] = "wait_api"
                res["attempts"] = attempts
                return res
            except AssertionError as e:
                if time.time() >= deadline:
                    raise AssertionError(
                        f"[wait_api {spec.get('query')}] 逾時 {timeout}s（試 {attempts} 次）仍未滿足條件：{e}")
                time.sleep(interval)

    @staticmethod
    def _dig(data: dict, dotted: str) -> Any:
        cur: Any = data
        for p in dotted.split("."):
            if isinstance(cur, list) and p.isdigit():
                cur = cur[int(p)]
            else:
                cur = cur[p]
        return cur
