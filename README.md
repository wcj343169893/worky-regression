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
| ④ 結果記錄器 | `recorder.py` | 逐步執行並落地到 `worky_qa_dashboard`（失敗不中斷記錄；缺 `id` 直接 raise；每跑產唯一 `run_id`） |

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

## QA 持久化（worky_qa_dashboard）

執行結果**只寫 DB**（不再產 `results/*.json`；舊檔留磁碟、已由 backfill 匯入）。
dashboard 的用例清單 / 詳情 / 步驟詳情全部從這個庫讀，排查時能精準定位
「哪個用例的哪一次跑、卡在哪一步」。

- **資料庫**：`worky_qa_dashboard`（與 worky 庫同 server，共用 host/port/user/pass；
  由 `.env` 的 `WORKY_QA_DB_NAME` 指定，預設 `worky_qa_dashboard`）。
- **三張表**：
  - `qa_cases` — 用例註冊（PK = 用例 id；含 file / system / source / yaml / step_count）。
  - `qa_runs` — 每次執行（PK = `run_id`；status / started_at / passed / total / failed_at / source）。
  - `qa_run_steps` — 每步（kind / status / elapsed_ms / error / observations(JSON)）。
- **id 規則**（排查關鍵）：
  - **用例 id**：YAML 的 `id:`；缺則用檔名 stem（`recorder` 缺 id 直接 raise，**不再 unnamed**）；
    AI 分解撞號自動加 `-2 / -3`。
  - **run_id**：`{case_id}-{started_at}-{hex}`（同秒多跑不撞；同 run_id 重入覆蓋＝冪等）。
- **schema＝SQLAlchemy 模型 + Alembic**：真實來源是 `qa_models.py` 的模型，**不要手寫 DDL**。
  改表流程：改模型 → `alembic revision --autogenerate -m "..."` → `alembic upgrade head`。
  dashboard / autotest / backfill 啟動時都會自動 `migrate()`（建庫 + `upgrade head`），平常免手動。
- **匯入舊資料**：`python -m worky_regression.qa_backfill`。

```bash
source .venv/bin/activate
python -m worky_regression.qa_backfill          # 把現有 results/*.json 灌進新庫
alembic upgrade head                            # 手動把 schema 帶到最新（平常啟動會自動跑）
alembic revision --autogenerate -m "<變更>"      # 改了 qa_models 模型後產生 migration
```

> `system` 是 MySQL 8.0 保留字，`qa_store` 的 raw SQL 對該欄一律加反引號 `` `system` ``。
> 後台用 `nohup` 起時加 `python -u`，否則 banner / log 會被緩衝看不到。

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
  recorder.py      # ★ RecordingRunner — 逐步記錄結果 → worky_qa_dashboard（每跑產 run_id）
  qa_models.py     # ★ QA 看板 schema 單一真實來源（SQLAlchemy 模型）+ migrate()
  qa_store.py      # ★ QAStore — 用例註冊 + 執行結果讀寫（SQLAlchemy engine + 顯式 SQL）
  qa_backfill.py   # ★ 一次性把舊 results/*.json 匯入 DB
  planner.py       # ★ DeepSeek 用例分解器（lean plan + 自動推導 expect）
  autotest.py      # ★ CLI：用例 → 任務流 → 執行 → 記錄
  dashboard/       # PC 任務看板（純檢視，stdlib HTTP）
    status.py / service.py / server.py / cases.py / static/

cases/
  _specs/endpoints.yaml          # ★ 介面/任務單元單一真實來源
  _fixtures/test_accounts.yaml   # audit 帳號（id/phone/user_type）
  path-*.yaml / job-*.yaml       # 一個檔 = 一條審批流路徑
  generated/                     # AI 分解器產出（gitignore；要保留就移上層）

alembic/           # QA 看板 schema 遷移（versions/ 為 autogenerate 產出）
alembic.ini
tests/
  test_smoke.py    # 環境連通性
  test_paths.py    # parametrize over cases/path-*.yaml
  test_jobs.py     # parametrize over cases/job-*.yaml
conftest.py        # session fixtures（settings/db/publisher/receiver/employer/labor）
results/           # 舊執行結果記錄（gitignore；已匯入 DB，現以 DB 為準）
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

### API 自助建帳號入池（無需工作庫權限）

框架沒有讀工作庫權限時，可純靠 API 自己造測試帳號入池。**dev/測試環境的註冊回應會直接帶
驗證碼**（`data.code`），所以全程 API 即可完成：產 `09` 開頭 10 位手機號 → 註冊 → 確認 →
補資料 → 讀 profile 取真實 id，寫進 `qa_accounts`（`note='api'`，與 audit 種子區分）。

```bash
source .venv/bin/activate
# 建 3 個打工夥伴入池（register → confirm → 補輪廓資料）
python -m worky_regression.qa_accounts register --role labor --n 3
# 建 2 個商家入池（register → confirm）
python -m worky_regression.qa_accounts register --role employer --n 2
python -m worky_regression.qa_accounts list        # 檢視（api 建的帳號 note 標 'api'）
```

看板「帳號池」頁也有「**＋ 註冊入池**」按鈕（對當前 tab 角色，單次上限 20）。

**caps 限制（重要）**：純 API 只能建到基本能力——
labor = `active` / `clean` / `profile_complete`，employer = `active`。
`verified`（實名認證）、`audit_role`（可被媒合的發佈角色）、`verified_shop`（店鋪送審+後台核）
**純 API 達不到**，需這些能力的用例仍用上方 audit 種子帳號。兩者並存於池中。

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

### 0. dev 環境 contract / job 分庫（現況：next-v31x）

- **job** 流程在 `WORKY_DB_NAME = worky_next_v31x`（22k+ `s_jobs`）。
- **contract** 流程：dev API 把 `s_contract_tasks` 等寫到 **`worky_next_staging_v30x`**
  （由 `WORKY_CONTRACT_DB_NAME` 指定）。`Settings.for_system()` 依 path 系統自動選庫。
  **改庫只動 `.env`，別寫死。**
- 兩套角色不同：contract 雙方皆 Labor（`user_type=2`）；job = employer（`user_type=1`）+ labor（`user_type=2`）。

### 1. PHP-FPM worker static 變數污染（Happy Path 卡關的根因）

`api/modules/v1/forms/contract/ReceiverMatchTaskForm::getTask()` 用了 `static $task = []`，
在 PHP-FPM 模式下會**跨 request 在同一 worker 內存活**。一旦某 `task_sn` 被 cache 成 `[]`，
同 worker 之後**所有**該 task 的請求都回空 → API 報 `50010「錯誤任務編號」`。

**症狀**：T1 剛建立的 task_sn，T2 馬上就找不到。
**繞過**：`sudo systemctl restart php8.2-fpm`（框架無法在 Python 端清）。
**根治建議**（值得另開 WKD 單）：改 instance property `private array $taskCache = []`。

### 2. Memcached `flush_all` 副作用

`flush_all` 清不掉 worker-local PHP `static`，反而可能讓 worker 對「不存在的 task」做出
錯誤緩存決定。runner 預設只在 `db_exec`（顯式 `flush_cache: true`）才 flush。

### 3. 付款方式：改用 ATM（v31x 已無 FunPoint 綁卡）

`s_contract_pay_fun_point_credit_cards` 在 v31x **整張空**，用 `payment_method_id=1`（信用卡）
會被擋 `20023`。`TaskPublishForm` 對 ATM（`payment_method_id=3`）只查金額上限、不需綁卡，
故 endpoints.yaml 的 T1 統一改 **`payment_method_id=3`**。ATM 原本「T6 卡住等付款」靠 happy path
用 `db_exec` 把 `pay_status` 改 `102`（PAYMENT_SUCCESS）繞過：

```yaml
- db_exec: >
    UPDATE s_contract_tasks SET pay_status=102,
      start_at=UNIX_TIMESTAMP()-60, end_at=UNIX_TIMESTAMP()+3600
    WHERE task_sn='{{state.task_sn}}'
  flush_cache: true
```

### 4. 任務時段條件

- contract：`start_time >= now + 86400s`（dev `MIN_PUBLISH_INTERVAL_SECONDS = 24h`）、
  `end_time - start_time ∈ [3600s, 30d]`、`start_time <= now + 90d`。
  runner 自動設 `start = now + 90000`（≈25h）、`end = start + 3700`，YAML 不用自己算。
  但 T6/T7 要求 `start_at <= now`、`end_at > now`，故發佈後須用 db_exec 把 `start_at/end_at`
  拉回當下（見 `cases/path-contract-happy-green.yaml` 橋接步驟）。
- job 系統時段由 `_job_slot_vars` 算（+3~+13 天）。

### 5. 發票 preflight（50045）

contract publish 要求發案者先設發票。`autotest.ensure_publisher_invoice()` 以 audit publisher
呼叫 `/contract/invoice/update` 寫最小設定（捐贈發票）；`_actors_for("contract")` 與 conftest 都會跑。

### 6. receiver 連續操作 1s 節流（9002）

`ReceiverTaskForm::validateTooFast()` 對同一 receiver 設 1s TTL 旗標，`T6→T7` < 1s 會回 `9002`。
runner 支援 `- sleep: <秒>` step，happy path 在 T6/T7 間插 `- sleep: 2`。

### 7. v31x 後端 J2 labor-apply 壞了（回歸框架已抓到）

J2（`/labor/job-match/job-apply`）回 `Setting unknown property: …LaborMatchJob::is_hidden_by_user_status`
——主倉 next-v31x 的 PHP 端 bug。**這是被測對象的 regression，不要在框架側修**，回報主倉即可。

### 8. 城市/區編號

不要亂填，需存在於 `s_districts`；contract T1 目前用 `city_id=19, district_id=193`。

---

## 加新 transition / case

1. **只動 `cases/_specs/endpoints.yaml`**（單一真實來源）新增任務單元，注意：
   - `endpoint` 對齊 `/www/wwwroot/worky/documents/api/` 文件
   - push `type_id` 對齊 `common\\components\\PushNotification\\Type` 常量
   - `transitions.py` / `push_type_ids.py` 已是 shim，**不要往裡塞資料**
2. 在 `cases/` 新增 `path-<scenario>.yaml`（或 `job-*.yaml`），列出 transition 序列與 expect
3. `pytest tests/test_paths.py -k <scenario>` 跑

YAML 模板可參考 `cases/path-contract-happy-green.yaml`。

---

## 跨倉資訊查詢

需要查 endpoint 文件、Event/Handler 程式碼、推播 Type 常量時，直接讀 `/www/wwwroot/worky/`：

- API 文件：`/www/wwwroot/worky/documents/api/<編號>-*.md`
- Event：`/www/wwwroot/worky/common/components/Contract/Event/After*.php`
- EventHandler：`/www/wwwroot/worky/common/components/Contract/EventHandler/After*/`
- PushNotification 子類：`.../Contract/EventHandler/After*/PushNotification.php`
- Type 常量：`/www/wwwroot/worky/common/components/PushNotification/Type.php`
- Consumer 屬性類：`.../common/components/RabbitMQ/Attribute/RabbitMQConsumer.php`
- OnEvent 屬性類：`.../common/components/EventTrigger/Attribute/OnEvent.php`
- ConfigLoader（自動掃描來源）：`.../common/helpers/ConfigLoader.php`

---

## 把 worker 跑起來

兩支背景 worker 與看板 server 解耦、可獨立起停。共通：先進虛擬環境、背景常駐用 `nohup` + `-u`
（不帶 `-u` 看不到即時 log，見 CLAUDE.md），log 落到 `logs/`。

```bash
source .venv/bin/activate
mkdir -p logs   # 首次背景常駐前
```

### 標記處理 worker（markup_worker）

處理看板「頁面標記(mark up)」：輪詢 `qa_markups` 的 `pending` → 把標記內容＋元素定位（＋回覆串）
組成 prompt 呼叫 headless `claude -p` → 依需求自動改看板代碼（或只回建議）→ 回寫 `result`、
狀態改 `done`/`failed`。看板頁面的標記框與徽章會即時反映狀態（前端輪詢）。

> 前置：`claude` CLI 已安裝且在 PATH（headless 跑）。**worker 沒跑時，標記送出後會一直停在
> 「待處理」**，因為沒有人領取處理。

```bash
# A) 只處理一筆就退出（除錯 / 想先看一筆效果）
python scripts/markup_worker.py --once

# B) 持續輪詢（無待處理時每 5s 探一次）
python scripts/markup_worker.py

# C) 背景常駐
nohup python -u scripts/markup_worker.py > logs/markup_worker.log 2>&1 &

# 只回建議、不讓 Claude 動檔（預設會帶 --dangerously-skip-permissions 自動改檔）
python scripts/markup_worker.py --no-skip-permissions
```

常用旗標：`--interval <秒>`（輪詢間隔，預設 5）、`--timeout <秒>`（單筆 claude 上限，預設 1800）、
`--once`、`--no-skip-permissions`。

### 帳號池補池 worker（account_pool_worker）

偵測各角色「可配發數」（state=available 或租約已過期），低於低標（預設 3）時先回收過期租約，
若仍不足才跑 `provision()`（解停權 / 上架 audit role + sync_caps）把流失的種子帳號救回。
池是固定 audit 種子帳號，不註冊新帳號，零侵入被測倉。

```bash
# 只檢查 / 補一次
python scripts/account_pool_worker.py --once

# 持續輪詢（預設 60s；--min-available 調低標，--no-heal 只回收+sync）
python scripts/account_pool_worker.py

# 背景常駐
nohup python -u scripts/account_pool_worker.py > logs/account_pool_worker.log 2>&1 &
```

### 查看 / 停止背景 worker

```bash
pgrep -af "markup_worker|account_pool_worker"   # 看哪支在跑（PID）
tail -f logs/markup_worker.log                  # 跟 log
kill <PID>                                       # 停止
```

---

## 環境變數（.env）

| 變數 | 預設 | 用途 |
|------|------|------|
| `WORKY_API_BASE` | `http://api.dev.worky.com.tw/v1` | 目標 API |
| `WORKY_API_SECRET` | (dev 共用) | 簽名 secret |
| `WORKY_AUDIT_SMS_CODE` | `9527` | audit 帳號固定碼 |
| `WORKY_DB_HOST/PORT/USER/PASS` | 192.168.101.213 / 3306 / root | DB 連線 |
| `WORKY_DB_NAME` | `worky_next_v31x` | job 流程驗證庫 |
| `WORKY_CONTRACT_DB_NAME` | `worky_next_staging_v30x` | contract 流程驗證庫（dev 分庫） |
| `WORKY_QA_DB_NAME` | `worky_qa_dashboard` | QA 看板庫（用例註冊 + 執行結果） |
| `WORKY_PLATFORM` | `WebPC` | header 用 |
| `DEEPSEEK_API_KEY / _BASE_URL / _MODEL` | — / api.deepseek.com / deepseek-chat | AI 用例分解器（Layer ③） |

---

## 未來工作

- [ ] PHP 端修掉 `static $task = []`，本框架就能跑通全部 happy path
- [ ] 補完 6 條 path，覆蓋 10 個 PushNotification 事件
- [ ] 補 push 內容（title / content）的字面斷言
- [ ] CI 整合（用 docker-compose 起獨立 worky 實例）
