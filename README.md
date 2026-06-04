# worky-regression

Worky 承攬制審批流回歸測試框架（Python + pytest）。

針對主倉 `worky` 的 `next-v30x` 分支重構驗證：

| Commit | 主題 |
|--------|------|
| `fc3ead87a` | RabbitMQ Consumer 由 `public const` 改為 `#[RabbitMQConsumer]` 屬性 (WKD-11050) |
| `b887687ee` | PusherOfTask 拆分為 10 個 PushNotification 類別 |
| `dd3612edf` | EventHandler 由靜態設定改為 `#[OnEvent]` 自聲明 |

主倉路徑：`/www/wwwroot/worky/`。本專案路徑：`/www/wwwroot/worky-regression/`。

---

## 設計概念：狀態機 = 審批流

承攬制是一個「**多角色狀態機**」：

```
[已發佈] ─申請(502-1)─→ [待審核] ─同意(407-1)─→ [已錄取] ─開始(506-1)─→ [執行中] ─結束(506-2)─→ [待審核] ─通過(408-2)─→ [已完成]
                          │                       │                                                    │
                          ├ 婉拒(407-2)            ├ 取消錄取(407-4)                                    └ 駁回(408-3) → loop
                          └ 接受邀請(502-4)         └ 接案者取消(505)
```

- 每個 transition = 一個 actor 對某個 endpoint 發起呼叫
- 每個 transition 觸發一個 Event，最終推播給對方
- 10 個 transition 對應 `PusherOfTask` 拆分後的 10 個 `PushNotification` 子類

**用例 = 在狀態機上挑一條路徑**。6 條 path 即可 100% 覆蓋 10 個事件。

承攬制的關鍵特殊性：「發案者」與「接案者」**都是 Labor**（`user_type=2`）。
所有 `/v1/contract/*` 端點都繼承 `LaborApiController`。

---

## 快速開始

```bash
cd /www/wwwroot/worky-regression
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env       # dev 設定已預填可直接用

# Smoke：DB 連線 + 兩個 audit 帳號登入
pytest tests/test_smoke.py -v

# Path 回歸（會打 dev 環境的 API + 寫 DB）
pytest tests/test_paths.py -v
```

跑單一 case：

```bash
pytest tests/test_paths.py -v -k path-t1-publish-task
```

---

## PC 任務看板（Dashboard）

純檢視的 Web 管理界面，直接讀 `worky_next_v30x` DB，一眼看到每個承攬制任務的
資訊與**當前進度**（媒合中 → 待付款 → 待開始 → 執行中 → 待確認 → 任務完成，
以及駁回 / 失敗 / 取消等分支）。零額外依賴（Python stdlib）。

```bash
source .venv/bin/activate
python -m worky_regression.dashboard          # 預設 http://127.0.0.1:8765
python -m worky_regression.dashboard --port 9000 --host 0.0.0.0
```

開瀏覽器進 `http://127.0.0.1:8765`。功能：

- **頂部統計**：總數 / 進行中 / 已完成 / 取消失敗，加一條進度分布長條。
- **任務清單**：task_sn、進度膠囊 + 迷你 stepper、金額、招募人數、時段、發案者；
  可搜尋（task_sn / 名稱）、依進度篩選、翻頁；可開「自動 15s 重新整理」。
- **詳情抽屜**（點任一列）：大型進度 stepper、原始 status/pay_status、接案者任務、
  申請媒合紀錄，以及由 `s_contract_task_change_logs` 串成的**進度時間軸**。
  每一進度階段都標注對應的回歸 transition（如「執行中 ← T6 開始任務」），把看板綁回測試框架。

> 進度碼邏輯移植自主倉 `common/base/Enums/Contract/PublisherTaskStatus.php`，
> 若主倉的 enum 變動，需同步 `dashboard/status.py`（與 `transitions.py`、`push_type_ids.py` 同樣的規矩）。
>
> 看板**只讀**，不對主倉或 DB 寫入；`display_name` 在 DB 為加密欄位，故角色以 phone + id 呈現。

---

## AI 用例分解 / 自動測試管線

把「手寫 YAML path」升級成**可由 AI 驅動**的測試編排，四層：

| 層 | 元件 | 職責 |
|----|------|------|
| ① 介面定義 | `cases/_specs/endpoints.yaml` | **單一真實來源**：每個任務單元的 request / response / 前置 / DB 副作用（enum）/ push |
| ② 任務單元 | `registry.py` | 從 endpoints.yaml 載入 → `Transition` registry + push type 全表 |
| ③ 用例分解器 | `planner.py` + `autotest.py` | DeepSeek（OpenAI 相容）把自然語言用例分解成任務流；**驗證由 spec 自動推導**，不靠 LLM 寫 SQL |
| ④ 結果記錄器 | `recorder.py` | 逐步執行並落地 `results/*.json`（失敗不中斷記錄） |

```bash
source .venv/bin/activate

# 用自然語言用例（需在 .env 設 DEEPSEEK_API_KEY；pip install -e .[ai]）
python -m worky_regression.autotest "商家發工作，夥伴申請後商家取消錄取"

# 只分解不執行，看產生的任務流
python -m worky_regression.autotest "..." --dry-run

# 跳過分解，直接跑既有 path YAML（走記錄器）
python -m worky_regression.autotest --path cases/job-happy-core.yaml
```

分解器只挑選 + 排序任務單元（必要時插 `db_exec` 時間/打卡碼橋接）；
框架再依 endpoints.yaml 的 `side_effects` / `push` **自動補上 `expect` 驗證**。
產出寫到 `cases/generated/<id>.yaml`（可檢視、編輯、移到 `cases/` 變正式用例）。

> **單一真實來源**：加 transition / 改 enum / 改 push type_id 只動 `endpoints.yaml`。
> 舊的 `transitions.py` / `push_type_ids.py` / `job_*` 已改為 re-export shim
> （消除「改兩個檔」的同步負擔）。

---

## 目錄結構

```
src/worky_regression/
  config.py        # .env 載入（Settings dataclass；含 DEEPSEEK_API_KEY）
  client.py        # WorkyClient — HTTP + 簽名 + headers
  actor.py         # Actor — 一個角色 = phone + user_id + 已登入的 client
  registry.py      # ★ 從 endpoints.yaml 建 Transition registry + push 全表（單一真實來源）
  transitions.py   # Transition dataclass（資料已移至 endpoints.yaml；其餘為相容 shim）
  push_type_ids.py # 相容 shim → registry
  verifier.py      # DBVerifier — query s_notifications / 業務表 / db_exec
  runner.py        # PathRunner — 讀 path → 執行 transitions → 三層驗證
  recorder.py      # ★ RecordingRunner — 逐步記錄結果 → results/*.json
  planner.py       # ★ DeepSeek 用例分解器（lean plan + 自動推導 expect）
  autotest.py      # ★ CLI：用例 → 任務流 → 執行 → 記錄
  dashboard/       # PC 任務看板（純檢視，stdlib HTTP）
    status.py / service.py / server.py / static/

cases/
  _specs/endpoints.yaml          # ★ 介面/任務單元單一真實來源
  _fixtures/test_accounts.yaml   # audit 帳號（id/phone/user_type）
  path-*.yaml / job-*.yaml       # 一個檔 = 一條審批流路徑
  generated/                     # AI 分解器產出（gitignore；要保留就移上層）

tests/
  test_smoke.py    # 環境連通性
  test_paths.py    # parametrize over cases/path-*.yaml
  test_jobs.py     # parametrize over cases/job-*.yaml
conftest.py        # session fixtures（settings/db/publisher/receiver/employer/labor）
results/           # 執行結果記錄（gitignore）
```

---

## 測試帳號

`worky_next_v30x` DB 中 `s_labor_roles.role_id=10 AND published=1` 的 audit labor：

| 角色 | id | phone | 狀態 |
|------|-----|-------|------|
| `publisher_primary` | 236 | 0923120600 | profile complete，已預綁信用卡 |
| `receiver_primary` | 276 | 0923113000 | profile complete |
| `receiver_backup` | 214 | 0900000001 | profile 未填，部份 endpoint 會擋 |

固定簡訊碼：**9527**（`WORKY_AUDIT_SMS_CODE`）。登入直接打 `/labor/login/confirm`，
body `{phone, password: md5("9527")}`，免發碼。

---

## 簽名規則（X-Worky-Signature）

```
md5(
    urlQueryString_sorted     # GET 才有；POST 留空
  + postBody_json_trimmed     # POST body 序列化結果
  + xWorkyCommonVariables     # 永遠帶
  + accessToken               # 匿名接口留空
  + apiSecret
)
```

詳見 `/www/wwwroot/worky/documents/api/001-API說明.md` 與本專案 `client.py`。

`session.trust_env = False` 是必要的 — 系統的 Privoxy 會吞 `.worky.com.tw` 的內網 domain。

---

## 驗證策略

每個 transition 跑完後，runner 做三層驗證：

1. **HTTP**：status code 不是 expected 立刻失敗
2. **業務層**：worky 統一回 `{success, code, data}`；`success=false` 視為失敗
3. **副作用**：
   - 推播驗證 → query `s_notifications` 比對 `type_id / uid / user_type / title / content`
   - 業務狀態 → query `s_contract_*` 表，比對特定欄位（YAML 中 `expect.state.sql + equals`）

`max_notification_id()` 在 transition 前抓 watermark，避免撈到歷史記錄。

---

## 已知限制與陷阱

### 1. PHP-FPM worker static 變數污染（Happy Path 卡關的根因）

`api/modules/v1/forms/contract/ReceiverMatchTaskForm::getTask()` 使用：

```php
private function getTask(string $taskSn): array
{
    static $task = [];
    if (isset($task[$taskSn])) return $task[$taskSn];
    // ... query DB ...
    $task[$taskSn] = $row ?: [];
    return $task[$taskSn];
}
```

`static` 變數在 PHP-FPM 模式下會**跨 request 在同一個 worker 內存活**。一旦
某次請求把某 `task_sn` cache 成 `[]`（task 還沒 commit 或 cache miss 回空），
同 worker 之後**所有**請求對該 task 都回空 → API 報 `50010「錯誤任務編號」`。

**症狀**：T1 剛建立的 task_sn，T2 馬上就找不到。

**繞過**：
- 重啟 php-fpm（需 sudo）：`sudo systemctl restart php8.2-fpm`
- 或讓 worker 自然輪替（idle 一段時間）

**根治建議**（值得另開 WKD 單）：把 `static $task = []` 改為 instance property
`private array $taskCache = []`，避免 worker-level 污染。

### 2. Memcached `flush_all` 副作用

`DBVerifier.flush_memcached()` 會清光整個 memcached。但**清不掉 worker-local
PHP `static`**，反而會讓 worker 對「不存在的 task」做出錯誤緩存決定。
runner 預設 `db_exec` step 才 flush，不在每個 transition 之間 flush。

### 3. ATM 付款卡關

`payment_method_id=3` (ATM) 在 T3a 之後會卡住 T6，因為需要 publisher 實際支付
才能 `start_task`。專案改用 `payment_method_id=1` (FunPoint 信用卡)：

- publisher 236 的 `s_pay_user_invoice_info` 已 INSERT 發票設定
- publisher 236 的 `s_contract_pay_fun_point_credit_cards` 已 clone 既有卡 binding

但 dev 環境 FunPoint 不會真扣款，path 中需要一個 `db_exec` step 做：
```sql
UPDATE s_contract_tasks SET pay_status=102 WHERE task_sn='{{state.task_sn}}'
```
並 `flush_cache: true`（清 model cache）。

### 4. 任務時段條件

- `start_time >= now + MIN_PUBLISH_INTERVAL_SECONDS`（dev = 720s ≈ 0.2h）
- `end_time - start_time >= 3600s`
- runner 預設 `start_time = now + 900`，`end_time = now + 900 + 3700`

`taskStart` / `taskEnd` service 內 stamp now，不檢查實際時間，所以連續跑沒問題。

### 5. 城市/區編號

不要亂填，需要存在於 `s_districts`。沿用 worky 既有任務最常見的 `city_id=19, district_id=194`。

---

## 加新 transition / case

1. 在 `transitions.py` 新增 `Transition(...)` 條目，注意：
   - `endpoint` 對齊 `/www/wwwroot/worky/documents/api/` 文件
   - `push_type_id` 對齊 `common\\components\\PushNotification\\Type` 常量
   - 若新類型 → 同步加進 `push_type_ids.py`
2. 在 `cases/` 新增 `path-<scenario>.yaml`，列出 transition 序列與 expect
3. `pytest tests/test_paths.py -k <scenario>` 跑

YAML 模板可參考 `cases/path-happy-publisher-pass.yaml`。

---

## 環境變數（.env）

| 變數 | 預設 | 用途 |
|------|------|------|
| `WORKY_API_BASE` | `http://api.dev.worky.com.tw/v1` | 目標 API |
| `WORKY_API_SECRET` | (dev 共用) | 簽名 secret |
| `WORKY_AUDIT_SMS_CODE` | `9527` | audit 帳號固定碼 |
| `WORKY_DB_HOST/PORT/USER/PASS/NAME` | 192.168.101.213 / worky_next_v30x | DB 驗證連線 |
| `WORKY_PLATFORM` | `WebPC` | header 用 |

---

## 未來工作

- [ ] PHP 端修掉 `static $task = []`，本框架就能跑通全部 happy path
- [ ] 補完 6 條 path，覆蓋 10 個 PushNotification 事件
- [ ] 補 push 內容（title / content）的字面斷言
- [ ] CI 整合（用 docker-compose 起獨立 worky 實例）
