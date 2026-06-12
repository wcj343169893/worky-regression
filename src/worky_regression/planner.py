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
from dataclasses import dataclass, field
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
        "system": {"type": "string", "enum": ["contract", "job", "activity"]},
        # 多夥伴/多身分用例需要的頂層變數，例：{job_recruit_count: 1, start_code: '000000'}
        "vars": {"type": "object",
                 "description": "頂層 vars：招募人數、打卡碼等覆寫，例 {job_recruit_count: 1}"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["transition", "db_exec"]},
                    "transition": {"type": "string",
                                   "description": "kind=transition 時的單元名，如 J3_employer_accept"},
                    # 步驟級身分重綁：多夥伴用例把某步的 labor 角色指向 labor1/labor2/labor3。
                    "bind": {"type": "object",
                             "description": "kind=transition 時切身分，例 {labor: labor2}"},
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
        # 前置顯示：優先用結構化 guards 的 need 描述，無則退回舊的 preconditions 字串列。
        guards = u.get("guards") or []
        pre = [g["need"] for g in guards if g.get("need")] or (u.get("preconditions") or [])
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
4. **不要產生任何 db_exec**：測試框架執行期一律不碰被測 DB（只打 API）。前置守衛由框架處理——
   時段衝突靠帳號池輪換自然避開；**打卡 / 任務開始結束等「時間鎖」前置無 API 可佈置，框架會
   自動把整支用例標 skip**（不可跑但覆蓋可見）。你只挑單元、排順序，**絕不要寫 kind=db_exec**。
5. **不要自己寫 expect 驗證**——框架會依 side_effects 自動補 push（打通知查詢 API）/ 狀態檢查。
   你只需挑單元、排順序。
6. **多夥伴/多身分用例**：job 系統有預設 `labor` 與三個具名夥伴 `labor1`/`labor2`/`labor3`
   （`labor` 即 `labor1`），contract 有 `publisher`/`receiver`。當用例出現「兩位夥伴各自申請」
   「A 申請、B 申請、商家錄取 A」這類**不同人**的動作時，必須在該 transition 步驟加
   `"bind": {{"labor": "labor2"}}` 切身分——**絕不可讓同一個 labor 重複申請同一份工作**
   （既語意錯誤，也會撞後端 9002「執行操作過快」）。被錄取/打卡的後續步驟綁回同一位（如 labor1）。
   ※ 同一身分連續動作的 9002 間隔由框架自動補，你不需要自己加 sleep。
7. **頂層 vars**：招募人數用 `"vars": {{"job_recruit_count": 1}}`（多人申請只錄取部分時必設）。
   ※ 含 J5/J6 打卡的路徑屬「時間鎖」，框架會自動標 skip，你照常挑單元即可、不需特別處理打卡碼。

# 菜單
{_render_menu()}

# 輸出
只輸出**單一 JSON 物件**（不要 markdown 圍欄、不要多餘文字），形如：
{{
  "path_id": "job-accept-one-of-two",
  "description": "商家發工作(招募1人)→兩位夥伴各自申請→商家錄取其中一位，驗證各階段狀態與推播。",
  "system": "job",
  "vars": {{"job_recruit_count": 1}},
  "steps": [
    {{"kind": "transition", "transition": "J1_employer_publish_job", "note": "建立工作"}},
    {{"kind": "transition", "transition": "J2_labor_apply", "bind": {{"labor": "labor1"}}, "note": "夥伴1申請"}},
    {{"kind": "transition", "transition": "J2_labor_apply", "bind": {{"labor": "labor2"}}, "note": "夥伴2申請"}},
    {{"kind": "transition", "transition": "J3_employer_accept", "bind": {{"labor": "labor1"}}, "note": "商家錄取夥伴1"}}
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
    vars: dict[str, Any] = field(default_factory=dict)


def _validate_plan(data: dict[str, Any]) -> None:
    """JSON mode 不在伺服器端強制 schema，這裡做最小驗證 + transition 名稱檢查。"""
    for key in ("path_id", "description", "system", "steps"):
        if key not in data:
            raise RuntimeError(f"分解器輸出缺少欄位 {key!r}：{data}")
    if data["system"] not in ("contract", "job", "activity"):
        raise RuntimeError(f"system 必須是 contract/job/activity，得到 {data['system']!r}")
    if "vars" in data and not isinstance(data["vars"], dict):
        raise RuntimeError(f"vars 必須是物件，得到 {type(data['vars']).__name__}")
    units = SPEC["task_units"]
    for i, st in enumerate(data["steps"]):
        kind = st.get("kind")
        if kind == "transition":
            name = st.get("transition")
            if name not in units:
                raise RuntimeError(f"steps[{i}] 未知 transition {name!r}（不在 endpoints.yaml）")
            bind = st.get("bind")
            if bind is not None and not isinstance(bind, dict):
                raise RuntimeError(f"steps[{i}] bind 必須是物件，例 {{labor: labor2}}")
        elif kind == "db_exec":
            if not st.get("sql"):
                raise RuntimeError(f"steps[{i}] db_exec 缺少 sql")
        else:
            raise RuntimeError(f"steps[{i}] kind 必須是 transition/db_exec，得到 {kind!r}")


# planner 目前實際支援分解的 system（其餘領域先標「規劃中」，由前端友善降級）
SUPPORTED_SYSTEMS = ("contract", "job", "activity")


def decompose(use_case: str, settings: Settings | None = None,
              system: str | None = None) -> TaskPlan:
    """呼叫 DeepSeek（OpenAI 相容介面）把 use_case 分解成 lean plan。需要 DEEPSEEK_API_KEY。

    用 JSON 輸出模式（response_format=json_object）並自行驗證；DeepSeek 對重複的
    system prompt 前綴有自動 context caching，菜單會被快取，不需手動 cache_control。

    ``system`` 為呼叫端（前端 tab）指定的目標系統：
      - 給定 ``job`` / ``contract`` 時，會在 user message 明確要求 LLM 輸出該 system，
        並在拿到 plan 後以「呼叫端指定」為準覆蓋 ``data["system"]`` 再驗證。
      - 給定 planner 尚未支援的 system（如 ``labor`` / ``employer``）時直接拋錯，
        讓前端做友善降級提示，不會送進 LLM。
      - ``None`` / 空字串時維持原行為（讓 LLM 自己判斷 system）。
    """
    # 呼叫端指定了目標 system：先做支援度檢查（不支援者直接擋下，不浪費 API 呼叫）
    sysname = (system or "").strip()
    if sysname and sysname not in SUPPORTED_SYSTEMS:
        raise RuntimeError(f"{sysname} 領域的 AI 分解尚未支援（規劃中）")

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

    # 指定 system 時在 user message 明確要求 LLM 輸出該 system
    user_msg = f"用例：{use_case}\n\n只輸出符合上述格式的 JSON。"
    if sysname:
        user_msg = f"用例：{use_case}\n\n目標 system 必須為 {sysname}。\n只輸出符合上述格式的 JSON。"

    client = OpenAI(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    resp = client.chat.completions.create(
        model=s.deepseek_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
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
    # 呼叫端指定了 system → 以指定為準覆蓋 LLM 回傳，再做驗證
    if sysname:
        data["system"] = sysname
    _validate_plan(data)
    # 空任務流：LLM 在 endpoints.yaml 菜單裡找不到對應單元（例如測的介面尚未建模）。
    # 不要靜默存一支 path:[] 的空用例——明確報錯，讓使用者知道「為何沒分解成功」。
    if not data["steps"]:
        raise RuntimeError(
            f"找不到對應的任務單元，無法分解「{use_case[:30]}」。"
            "目前 endpoints.yaml 只建模了承攬制（T*）與工作系統（J*）的端點；"
            "若要測試此介面，需先把它的端點加進 cases/_specs/endpoints.yaml。"
        )
    return TaskPlan(path_id=data["path_id"], description=data["description"],
                    system=data["system"], steps=data["steps"], raw=data,
                    vars=data.get("vars") or {})


# ── AI 建立分解 tab（依自然語言描述產生一個領域 tab 的設定）──────────────────
# tab.system 允許的值：job/contract（可分解）、labor/employer（帳號生命週期，分解規劃中）、
# ""（全部，不指定系統）。其餘一律正規化為 ""。
TAB_SYSTEMS = ("job", "contract", "activity", "labor", "employer", "")

_TAB_SYSTEM_PROMPT = """你是 Worky 回歸測試看板的助理。使用者用一句話描述他想針對哪一類功能\
建立「AI 用例分解 tab」。請把它歸類並產生 tab 設定，只輸出 JSON：
{
  "label": "<簡短中文 tab 名，2-6 字>",
  "system": "<job|contract|activity|labor|employer 或空字串>",
  "query": "<過濾既有用例清單的關鍵字，取描述中最具辨識度的詞，1-6 字>",
  "placeholder": "<切到此 tab 時輸入框的提示語，引導使用者描述該領域用例>"
}
system 對映：工作流程=job；承攬任務流程=contract；營運/行銷活動（點石成金、排行榜、MGM、\
加薪任務、活動橫幅等）=activity；打工夥伴帳號（註冊/審核等）=labor；商家/店鋪=employer；\
無法判定填空字串。只輸出 JSON，不要任何解釋。"""


def _normalize_tab(data: dict[str, Any], desc: str) -> dict[str, Any]:
    """把 LLM / 啟發式產出的 tab 設定正規化成穩定形狀（防缺欄、限制 system 值）。"""
    system = str(data.get("system") or "").strip().lower()
    if system not in TAB_SYSTEMS:
        system = ""
    label = (str(data.get("label") or "").strip() or desc[:6]) or "自訂"
    query = str(data.get("query") or "").strip() or desc[:12]
    placeholder = (str(data.get("placeholder") or "").strip()
                   or f"描述「{label}」相關的測試用例…")
    return {"label": label[:16], "system": system,
            "query": query[:24], "placeholder": placeholder[:80]}


def _suggest_tab_heuristic(desc: str) -> dict[str, Any]:
    """無 API key / 呼叫失敗時的關鍵字啟發式（盡量別讓功能整個失效）。

    用「命中數計分取最高」而非「首個命中」，避免泛詞（如「打工」「審核」）造成誤判；
    全部 0 分則回 ""（全部）。關鍵字刻意避開跨領域歧義詞。
    """
    kw = {
        "contract": ["任務", "承攬", "接案", "發案", "驗收", "派工"],
        "job": ["工作", "時薪", "招募", "上工", "打卡", "排班"],
        "activity": ["活動", "營運", "點石成金", "排行榜", "排名", "中獎", "獎金", "MGM", "加薪任務"],
        "labor": ["夥伴", "註冊", "實名", "帳號", "個資", "登入"],
        "employer": ["商家", "店鋪", "門市", "開店", "分店", "店家"],
    }
    scores = {sys_name: sum(w in desc for w in words) for sys_name, words in kw.items()}
    best = max(scores, key=lambda k: scores[k])
    system = best if scores[best] > 0 else ""
    return _normalize_tab({"system": system}, desc)


def _suggest_tab_ai(desc: str, s: Settings) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    resp = client.chat.completions.create(
        model=s.deepseek_model,
        messages=[
            {"role": "system", "content": _TAB_SYSTEM_PROMPT},
            {"role": "user", "content": f"描述：{desc}\n\n只輸出符合上述格式的 JSON。"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        stream=False,
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    return _normalize_tab(data if isinstance(data, dict) else {}, desc)


def suggest_tab(description: str, settings: Settings | None = None) -> dict[str, Any]:
    """依自然語言描述產生一個 AI 用例分解 tab 的設定。

    回傳 ``{label, system, query, placeholder}``。有 DEEPSEEK_API_KEY 時走 LLM 歸類，
    失敗或無 key 時退回關鍵字啟發式（不丟例外，盡量讓「新增 tab」可用）。
    """
    desc = (description or "").strip()
    if not desc:
        raise RuntimeError("描述不可為空")
    s = settings or Settings.from_env()
    if s.deepseek_api_key:
        try:
            return _suggest_tab_ai(desc, s)
        except Exception:  # noqa: BLE001 — LLM 失敗退回啟發式，不讓功能整個壞掉
            pass
    return _suggest_tab_heuristic(desc)


# ── AI 失敗分析（step modal 失敗時的「分析」按鈕）────────────────────────────
_ANALYZE_SYSTEM_PROMPT = """你是 Worky 承攬制/工作系統回歸測試的失敗診斷助理。\
使用者會給你一個「失敗步驟」的結構化情境（被測端點、預期、實際 error、observations、\
HTTP 狀態、操作者角色等）。請判斷最可能的失敗原因並給出可執行建議，只輸出 JSON：
{
  "cause": "<一句話點出最可能的根因，繁體中文>",
  "detail": "<2-4 句說明你的推理：對齊 error / observations / expect 哪裡不符>",
  "suggestion": "<建議下一步，繁體中文>",
  "recommended_action": "<retry|swap|inspect|report>"
}
recommended_action 對映：
- retry：疑似環境/時序/快取污染等暫時性問題（如 PHP-FPM static 污染、receiver 過快、memcached），重跑可能就過。
- swap：疑似帳號硬狀態問題（停權、能力不符、該帳號殘留資料），換一個同能力帳號重跑。
- inspect：需人工看 YAML / 端點規格 / DB，無法靠重跑或換號解決。
- report：疑似被測對象（worky 主倉）的 bug，應回報主倉而非在框架側處理。
只輸出 JSON，不要任何解釋或 markdown 圍欄。"""


def analyze_failure(context: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """把失敗步驟情境餵給 DeepSeek，回傳 {cause, detail, suggestion, recommended_action}。

    需要 DEEPSEEK_API_KEY；無 key 直接拋 RuntimeError（由 server 轉成可讀錯誤）。
    """
    s = settings or Settings.from_env()
    if not s.deepseek_api_key:
        raise RuntimeError("未設定 DEEPSEEK_API_KEY（分析器停用）。請在 .env 加 DEEPSEEK_API_KEY=...")
    try:
        from openai import OpenAI
    except ModuleNotFoundError as e:
        raise RuntimeError("未安裝 openai SDK，請 `pip install -e .[ai]`") from e

    client = OpenAI(api_key=s.deepseek_api_key, base_url=s.deepseek_base_url)
    resp = client.chat.completions.create(
        model=s.deepseek_model,
        messages=[
            {"role": "system", "content": _ANALYZE_SYSTEM_PROMPT},
            {"role": "user", "content": "失敗步驟情境：\n" + json.dumps(context, ensure_ascii=False, indent=2)
             + "\n\n只輸出符合上述格式的 JSON。"},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        stream=False,
    )
    text = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"分析器回傳非合法 JSON：{e}\n原文：{text[:300]}") from e
    action = str(data.get("recommended_action") or "inspect").strip().lower()
    if action not in ("retry", "swap", "inspect", "report"):
        action = "inspect"
    return {
        "cause": str(data.get("cause") or "").strip() or "（模型未給出明確根因）",
        "detail": str(data.get("detail") or "").strip(),
        "suggestion": str(data.get("suggestion") or "").strip(),
        "recommended_action": action,
    }


def _expect_from_unit(name: str) -> dict[str, Any]:
    """從 spec 的 push / verify_api 自動推導一個 transition 步驟的 expect。

    狀態驗證一律走查詢端點（unit 的 ``verify_api`` → expect.api），不再從 side_effects
    合成 SELECT（expect 驗證不查 SQL；side_effects 保留作文件/對照用途）。
    無 verify_api 的 transition 只驗 http（+push），不留 SQL 後門。
    """
    import copy

    u = unit_spec(name)
    expect: dict[str, Any] = {"http": 200}
    if u.get("push"):
        expect["push"] = {}                       # 空 → 只驗 type_id 落地
    if u.get("verify_api"):
        expect["api"] = copy.deepcopy(u["verify_api"])
    return expect


def _effective_actor(name: str, bind: dict[str, Any] | None) -> str:
    """這步實際發請求的身分識別字串：單元 actor 角色，被 bind 重綁時取綁定目標。

    例：J2_labor_apply 角色為 labor → 無 bind 回 'labor'；bind={labor: labor2} 回 'labor2'。
    用於偵測「同一身分連續呼叫」以自動補 sleep（繞後端 9002 過快節流）。
    """
    role = unit_spec(name)["actor"]
    if bind and role in bind:
        return str(bind[role])
    return role


def _guard_skips(name: str) -> list[str]:
    """endpoints.yaml 單元 guards 裡標記為無法自動化（時間鎖 / 無 API 可替代）的 skip 原因。

    #4 後 guards 不再放 db_exec（執行期不碰 DB）：原本要靠 SQL 壓縮時間 / 預寫打卡碼 /
    清殘留的前置，改用 satisfy.skip 標記。build_path 據此把整支用例標 skip（不可跑但覆蓋可見）。
    """
    return [r for g in (unit_spec(name).get("guards") or [])
            if (r := (g.get("satisfy") or {}).get("skip"))]


def build_path(plan: TaskPlan) -> dict[str, Any]:
    """把 lean plan 展開成 PathRunner 吃的完整 path dict（自動補 expect / bind / sleep / vars）。

    - bind：步驟級身分重綁原樣透傳（多夥伴用例切 labor1/labor2）。
    - sleep：偵測「同一身分連續呼叫」自動插入 ``sleep: 2``（後端對同 actor ~1s 內第二次
      動作回 9002「執行操作過快」）；中間隔著 db_exec 不算已間隔（耗時不足）。
    - vars：plan 頂層 vars 原樣帶到 path 頂層（招募人數、打卡碼等）。
    """
    steps: list[dict[str, Any]] = []
    skip_reasons: list[str] = []           # 任一 transition 的 guard 無法自動化 → 整支 skip
    last_txn_actor: str | None = None      # 上一個 transition 的實際身分
    spaced = True                          # 自上個 transition 以來是否已有 sleep 間隔
    for st in plan.steps:
        if st["kind"] == "db_exec":
            # #4：執行期不碰 DB。LLM 不應再產 db_exec；若仍出現，原樣帶上由 runner 報 DBAccessDisabled。
            step: dict[str, Any] = {"db_exec": st["sql"]}
            if "flush_cache" in st:
                step["flush_cache"] = st["flush_cache"]
            steps.append(step)
            continue
        name = st["transition"]
        bind = st.get("bind") or None
        actor = _effective_actor(name, bind)
        # 同一身分連續兩次動作且中間無 sleep → 補 2s，避開後端 9002 過快節流。
        if last_txn_actor is not None and actor == last_txn_actor and not spaced:
            steps.append({"sleep": 2})
            spaced = True
        # 該 transition 的 guards 若有無法自動化的（時間鎖等）→ 收集成用例層 skip 原因。
        for r in _guard_skips(name):
            if r not in skip_reasons:
                skip_reasons.append(r)
        u = unit_spec(name)
        step = {"transition": name}
        if bind:
            step["bind"] = dict(bind)
        if u.get("saves"):
            step["save"] = dict(u["saves"])
        step["expect"] = _expect_from_unit(name)
        steps.append(step)
        last_txn_actor, spaced = actor, False
    out: dict[str, Any] = {"id": plan.path_id, "description": plan.description, "path": steps}
    if plan.vars:
        out["vars"] = dict(plan.vars)
    if skip_reasons:
        out["skip"] = True
        out["skip_reason"] = "；".join(skip_reasons)
    return out


def plan_to_json(plan: TaskPlan) -> str:
    return json.dumps(plan.raw, ensure_ascii=False, indent=2)
