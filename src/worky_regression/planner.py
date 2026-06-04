"""Layer ③ — DeepSeek（OpenAI 相容介面）用例分解器。

把一句「用例」自然語言（例如「商家發工作，夥伴申請後商家又取消錄取」）
分解成一條**任務流**：有序的任務單元序列（+ 必要的 db_exec 時間/打卡碼橋接）。

設計重點
--------
- 分解器只負責**挑選 + 排序任務單元**；它讀 endpoints.yaml 當「菜單」（cached）。
- **驗證（expect.push / expect.state）由框架從 spec 的 side_effects 自動推導**，
  不要 LLM 寫 SQL —— 驗證一律以單一真實來源為準，避免 LLM 亂編欄位。
- 產物就是 PathRunner / RecordingRunner 吃的同一份 path dict，可被人檢視/編輯。

無 DEEPSEEK_API_KEY 時 ``decompose`` 會丟 RuntimeError；其餘框架不受影響。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .registry import SPEC, unit_spec

# 分解器輸出的 JSON schema（lean plan：只挑單元 + 排序 + db_exec 橋接）
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path_id": {"type": "string", "description": "kebab-case 短名，如 job-cancel-hire"},
        "description": {"type": "string", "description": "這條 path 在測什麼（繁中一兩句）"},
        "system": {"type": "string", "enum": ["contract", "job"]},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["transition", "db_exec"]},
                    "transition": {"type": "string",
                                   "description": "kind=transition 時的單元名，如 J3_employer_accept"},
                    "sql": {"type": "string",
                            "description": "kind=db_exec 時的 SQL，可用 {{state.job_sn}} 等變數"},
                    "flush_cache": {"type": "boolean"},
                    "note": {"type": "string", "description": "這步為何存在"},
                },
                "required": ["kind"],
            },
        },
    },
    "required": ["path_id", "description", "system", "steps"],
}


def _render_menu() -> str:
    """把 SPEC 壓成精簡菜單字串（給 system prompt 當 cached context）。"""
    lines: list[str] = []
    for sysname, meta in SPEC["systems"].items():
        roles = "、".join(f"{r}（{d}）" for r, d in meta["roles"].items())
        lines.append(f"## 系統 {sysname}：{meta['label']}　角色：{roles}　主鍵：{{{{state.{meta['key_var']}}}}}")
    lines.append("\n## 任務單元（最小可執行單位）")
    for name, u in SPEC["task_units"].items():
        push = u.get("push") or {}
        push_s = f"｜推播→{push['to']}({push['type']})" if push else ""
        saves = f"｜save {u['saves']}" if u.get("saves") else ""
        pre = u.get("preconditions") or []
        pre_s = f"｜前置：{'；'.join(pre)}" if pre else ""
        lines.append(f"- {name} [{u['system']}/{u['actor']}] {u['summary']}{push_s}{saves}{pre_s}")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""你是 Worky 回歸測試框架的「用例分解器」。把使用者的測試用例（自然語言）
分解成一條可依序執行的**任務流**，只用下方菜單裡的任務單元。

# 規則
1. 這是**狀態機**，順序不能跳關。一條 path 必須先用建立單元產生主體：
   - contract 系統先 T1_publisher_publish_task（產生 task_sn）。
   - job 系統先 J1_employer_publish_job（產生 job_sn）。
2. 一條 path 只能屬於單一系統（contract 或 job），角色不可混用。
3. 遵守每個單元的「前置」。例如：J3 同意上工前，夥伴必須先 J2 申請；
   J4 取消錄取前必須先 J3 錄取；J5 打卡前必須先 J3 錄取。
4. 需要操控時間或打卡碼時，插入 kind=db_exec 步驟（SQL 可用 {{{{state.job_sn}}}} /
   {{{{labor.user_id}}}} 變數）。典型：
   - J5/J6 打卡前：UPDATE s_labor_jobs SET start_code='000000', end_code='000000',
     end_at=UNIX_TIMESTAMP()+3600 WHERE job_id=(SELECT id FROM s_jobs WHERE
     job_sn='{{{{state.job_sn}}}}') AND labor_id={{{{labor.user_id}}}}（並 flush_cache=true），
     再加一個 db_exec 把打卡碼存進 state：用 SELECT 取碼不可行，請直接在 J5/J6
     的 code 用固定值，所以打卡碼要先 UPDATE 成你在路徑裡會用的值。
   - 注意：打卡碼 code 由單元的 request 模板用 {{{{state.start_code}}}}/{{{{state.end_code}}}}
     帶入，但 runner 不會自動產生它；若你要測 J5/J6，務必在 note 標明需要框架補 state。
5. **不要自己寫 expect 驗證**——框架會依 side_effects 自動補 push / DB 狀態檢查。
   你只需挑單元、排順序、必要時加 db_exec。
6. db_exec 修改 DB 後一般要 flush_cache=true（worky 有 memcached）。
7. **不要自己寫 expect 驗證**——框架會依 side_effects 自動補 push / DB 狀態檢查。

# 菜單
{_render_menu()}

# 輸出
只輸出**單一 JSON 物件**（不要 markdown 圍欄、不要多餘文字），形如：
{{
  "path_id": "job-cancel-hire",
  "description": "商家發工作→夥伴申請→錄取→取消錄取，驗證各階段狀態與推播。",
  "system": "job",
  "steps": [
    {{"kind": "transition", "transition": "J1_employer_publish_job", "note": "建立工作"}},
    {{"kind": "transition", "transition": "J2_labor_apply", "note": "夥伴申請"}},
    {{"kind": "db_exec", "sql": "UPDATE s_labor_jobs SET start_code='000000' WHERE ...", "flush_cache": true, "note": "塞打卡碼"}}
  ]
}}
transition 名稱必須與菜單完全一致。
"""


@dataclass
class TaskPlan:
    path_id: str
    description: str
    system: str
    steps: list[dict[str, Any]]
    raw: dict[str, Any]


def _validate_plan(data: dict[str, Any]) -> None:
    """JSON mode 不在伺服器端強制 schema，這裡做最小驗證 + transition 名稱檢查。"""
    for key in ("path_id", "description", "system", "steps"):
        if key not in data:
            raise RuntimeError(f"分解器輸出缺少欄位 {key!r}：{data}")
    if data["system"] not in ("contract", "job"):
        raise RuntimeError(f"system 必須是 contract/job，得到 {data['system']!r}")
    units = SPEC["task_units"]
    for i, st in enumerate(data["steps"]):
        kind = st.get("kind")
        if kind == "transition":
            name = st.get("transition")
            if name not in units:
                raise RuntimeError(f"steps[{i}] 未知 transition {name!r}（不在 endpoints.yaml）")
        elif kind == "db_exec":
            if not st.get("sql"):
                raise RuntimeError(f"steps[{i}] db_exec 缺少 sql")
        else:
            raise RuntimeError(f"steps[{i}] kind 必須是 transition/db_exec，得到 {kind!r}")


def decompose(use_case: str, settings: Settings | None = None) -> TaskPlan:
    """呼叫 DeepSeek（OpenAI 相容介面）把 use_case 分解成 lean plan。需要 DEEPSEEK_API_KEY。

    用 JSON 輸出模式（response_format=json_object）並自行驗證；DeepSeek 對重複的
    system prompt 前綴有自動 context caching，菜單會被快取，不需手動 cache_control。
    """
    s = settings or Settings.from_env()
    if not s.deepseek_api_key:
        raise RuntimeError(
            "未設定 DEEPSEEK_API_KEY（分解器停用）。請在 .env 加 DEEPSEEK_API_KEY=...，"
            "或改用 --plan <file.json> 直接給手寫任務流。"
        )
    try:
        from openai import OpenAI
    except ModuleNotFoundError as e:
        raise RuntimeError("未安裝 openai SDK，請 `pip install -e .[ai]`") from e

    client = OpenAI(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    resp = client.chat.completions.create(
        model=s.deepseek_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"用例：{use_case}\n\n只輸出符合上述格式的 JSON。"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        stream=False,
    )
    text = resp.choices[0].message.content or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"分解器回傳非合法 JSON：{e}\n原文：{text[:500]}") from e
    _validate_plan(data)
    return TaskPlan(path_id=data["path_id"], description=data["description"],
                    system=data["system"], steps=data["steps"], raw=data)


def _expect_from_unit(name: str) -> dict[str, Any]:
    """從 spec 的 push / side_effects 自動推導一個 transition 步驟的 expect。"""
    u = unit_spec(name)
    expect: dict[str, Any] = {"http": 200}
    if u.get("push"):
        expect["push"] = {}                       # 空 → 只驗 type_id 落地
    for se in (u.get("side_effects") or []):
        become = se.get("become") or {}
        if become:                                 # runner 一步只驗一個 state query
            cols = ", ".join(become.keys())
            expect["state"] = {
                "sql": f"SELECT {cols} FROM {se['table']} WHERE {se['key']}",
                "equals": dict(become),
            }
            break
    return expect


def build_path(plan: TaskPlan) -> dict[str, Any]:
    """把 lean plan 展開成 PathRunner 吃的完整 path dict（自動補 expect）。"""
    steps: list[dict[str, Any]] = []
    for st in plan.steps:
        if st["kind"] == "db_exec":
            step: dict[str, Any] = {"db_exec": st["sql"]}
            if "flush_cache" in st:
                step["flush_cache"] = st["flush_cache"]
            steps.append(step)
            continue
        name = st["transition"]
        u = unit_spec(name)
        step = {"transition": name}
        if u.get("saves"):
            step["save"] = dict(u["saves"])
        step["expect"] = _expect_from_unit(name)
        steps.append(step)
    return {"id": plan.path_id, "description": plan.description, "path": steps}


def plan_to_json(plan: TaskPlan) -> str:
    return json.dumps(plan.raw, ensure_ascii=False, indent=2)
