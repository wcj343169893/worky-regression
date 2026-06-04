# CLAUDE.md — worky-regression

所有回答使用**繁體中文**。

## 專案定位

Worky 承攬制審批流回歸測試框架（Python + pytest）。
不是業務專案，是用來驗證主倉 `worky` 重構正確性的測試工具。

- 主倉：`/www/wwwroot/worky/`（PHP/Yii2，分支 `next-v30x`）
- 本專案：`/www/wwwroot/worky-regression/`（Python ≥ 3.10）
- 目標 API：`http://api.dev.worky.com.tw/v1`
- 驗證 DB：`worky_next_v30x @ 192.168.101.213`

## 核心觀念

承攬制 = **多角色狀態機 / 審批流**。一個任務的生命週期由 publisher 與 receiver
（**都是 Labor，`user_type=2`**）輪流操作 endpoint 推進。

每個 transition：
1. actor 呼叫 endpoint（簽名 + access token）
2. 後端發 Event → handler 寫 `s_notifications` 推播
3. runner 三層驗證：HTTP / 業務 success / DB 副作用

**用例 = YAML 描述的 transition 序列**。詳見 `README.md` 設計章節。

## 程式碼結構速查

| 檔案 | 職責 |
|------|------|
| `src/worky_regression/config.py` | `.env` → `Settings` dataclass |
| `src/worky_regression/client.py` | `WorkyClient` HTTP + 簽名（md5 of `query+body+commonVars+token+secret`） |
| `src/worky_regression/actor.py` | `Actor` 登入 + token 管理 |
| `cases/_specs/endpoints.yaml` | **單一真實來源**：每個任務單元的 request/response/前置/DB 副作用 enum/push（contract T* + job J*） |
| `src/worky_regression/registry.py` | 從 endpoints.yaml 建 `Transition` registry + push type 全表 |
| `src/worky_regression/transitions.py` | `Transition` dataclass（資料已移至 endpoints.yaml；`push_type_ids.py`/`job_*` 皆為相容 shim） |
| `src/worky_regression/verifier.py` | `DBVerifier`：watermark、`assert_push`、`execute`、`flush_memcached` |
| `src/worky_regression/runner.py` | `PathRunner` — 讀 path，展開 `{{state.xxx}}` / `{{publisher.user_id}}` |
| `src/worky_regression/recorder.py` | `RecordingRunner` — 逐步記錄結果 → `results/*.json`（失敗不中斷記錄） |
| `src/worky_regression/planner.py` | DeepSeek（OpenAI 相容）用例分解器（lean plan；expect 由 spec 自動推導，不靠 LLM 寫 SQL） |
| `src/worky_regression/autotest.py` | CLI：用例 → 任務流 → 執行 → 記錄（`python -m worky_regression.autotest "<用例>"`） |
| `conftest.py` | session fixtures：`settings/db/publisher/receiver/employer/labor`；publisher 自動寫發票 |
| `cases/path-*.yaml` / `cases/job-*.yaml` | 一個檔 = 一條 path（承攬制 / 工作系統） |
| `cases/_fixtures/test_accounts.yaml` | audit 帳號 |
| `src/worky_regression/dashboard/` | PC 任務看板（純檢視 Web，stdlib HTTP）：`status.py` 進度碼移植 / `service.py` DB 查詢 / `server.py` 路由 / `static/` SPA。啟動：`python -m worky_regression.dashboard` |

## 開發/驗證流程

```bash
# 進入 venv
source .venv/bin/activate

# Smoke（必須先過）
pytest tests/test_smoke.py -v

# 跑單一 path
pytest tests/test_paths.py -v -k <path-stem>

# 跑全部 path
pytest tests/test_paths.py -v
```

## 工作慣例

- **不要刪 audit 帳號的綁卡 / 發票**（手工建的，重建很麻煩）：
  - `s_pay_user_invoice_info` (publisher 236)
  - `s_contract_pay_fun_point_credit_cards` (publisher 236)
- **YAML path 步驟順序很重要**，狀態機不能跳關
- 加新 transition / 改 enum / 改 push type_id：**只動 `cases/_specs/endpoints.yaml`**（單一真實來源；
  `transitions.py` / `push_type_ids.py` 已是 shim，不要再往裡塞資料）
- endpoint 路徑以 `/www/wwwroot/worky/documents/api/` 為準
- push type_id 以 `/www/wwwroot/worky/common/components/PushNotification/Type.php` 為準
- 提交時 commit message 格式：`<type>: <描述>(WKD-XXXXX)`（沿用 worky 慣例；若該變動有對應 Jira 單再帶）

## 已知陷阱（重要！）

### 環境已切到 next-v31x（工作系統）

**dev 環境 contract 與 job 分屬不同 DB**（已用 regression 實測確認）：
- **job** 流程在 `WORKY_DB_NAME = worky_next_v31x`（22k+ `s_jobs`）。
- **contract** 流程：dev API 實際把 `s_contract_tasks` 等寫到 **`worky_next_staging_v30x`**
  （v31x 的 `s_contract_tasks` 是 0 筆）。框架用 `WORKY_CONTRACT_DB_NAME` 指定，
  `Settings.for_system("contract")` 自動切庫；run 管線（autotest / dashboard CaseStore）
  依 path 系統選 DB。**改庫只動 `.env`，別寫死。**

兩套流程角色不同（contract 雙方皆 Labor；job = employer user_type=1 + labor user_type=2）。

### 承攬制發任務需「24 小時後」開始（已修）

T1 contract publish 規則（`TaskPublishForm`）：`start_time >= now + 86400`（MIN_PUBLISH_INTERVAL）
且 `start_time - 86400 >= now`（招募截止）、`3600 <= end-start <= 30d`、`<= now+90d`。
**已修**：`runner.init_state` 改注入 `start_time = now + 90000`（≈25h，含 1h buffer 防時鐘飄移）。
但 T6/T7 要求 `start_at <= now`、`end_at > now`，故發佈後須用 db_exec 把 `start_at/end_at`
拉回當下（見 `cases/path-contract-happy-green.yaml` 的橋接步驟）。

### v31x 後端 J2 labor-apply 壞了（回歸框架已抓到）

J2（`/labor/job-match/job-apply`）回 `Setting unknown property:
api\models\Labor\LaborMatchJob::is_hidden_by_user_status`——主倉 next-v31x 的 PHP 端 bug，
J1 發佈正常但 J2 申請就炸。**這是被測對象的 regression，不要在框架側修**，回報主倉即可。

### PHP-FPM worker static 污染

`api/modules/v1/forms/contract/ReceiverMatchTaskForm::getTask()` 用了 `static $task = []`，
在 PHP-FPM 模式下會跨 request 持續於同一 worker。一旦 cache 到 `[]`，後續所有
同 task_sn 的請求都會繼續回空。

**症狀**：T1 剛建立的 task，T2 立刻找不到（`50010 錯誤任務編號`）。

**繞過**：請 user 跑 `sudo systemctl restart php8.2-fpm`，本框架沒辦法在 Python 端清掉。

**根治**（值得另開 WKD 單修主倉）：改 instance property `private array $taskCache = []`。

### Memcached flush 副作用

`flush_all` 不會清 PHP `static`，反而可能讓 worker 進入「task 不存在」的錯誤狀態。
runner 預設只在 `db_exec` step（顯式宣告 `flush_cache: true`）才 flush。

### 付款方式：改用 ATM（v31x 已無 FunPoint 綁卡）

`s_contract_pay_fun_point_credit_cards` 在 v31x 庫**整張空**（舊 v30x 的 publisher 236
綁卡已不存在），用 `payment_method_id=1`（信用卡）發佈會被擋 `20023 需先設定信用卡`。
`TaskPublishForm` 對 ATM（`payment_method_id=3`）只查金額上限、不需綁卡，故 endpoints.yaml
的 T1 已統一改 **`payment_method_id=3`**。ATM 原本「T6 卡住等付款」的問題，靠 happy path
一律用 db_exec 把 `pay_status` 改 `102`（PAYMENT_SUCCESS）繞過：

```yaml
- db_exec: >
    UPDATE s_contract_tasks SET pay_status=102,
      start_at=UNIX_TIMESTAMP()-60, end_at=UNIX_TIMESTAMP()+3600
    WHERE task_sn='{{state.task_sn}}'
  flush_cache: true
```

### 發票 preflight（50045）

contract publish 要求發案者先設發票，否則 `50045 尚未設定發票資訊`。
`autotest.ensure_publisher_invoice()` 會以 audit publisher 呼叫 `/contract/invoice/update`
寫最小設定（捐贈發票）；`_actors_for("contract")` 與 conftest 都會跑這個 preflight。

### receiver 連續操作 1s 節流（9002）

`ReceiverTaskForm::validateTooFast()` 對同一 receiver 設 1s TTL 旗標，T6→T7 若 < 1s
會回 `9002 執行操作過快`。runner 已支援 **`- sleep: <秒>`** step，happy path 在 T6/T7 間插
`- sleep: 2`。

### 時段條件

- contract：`start_time >= now + 86400s`（dev `MIN_PUBLISH_INTERVAL_SECONDS = 24h`）、
  `end_time - start_time ∈ [3600s, 30d]`、`start_time <= now + 90d`
- runner 自動設 contract `start = now + 90000`（≈25h）、`end = start + 3700`，YAML 不用自己算；
  job 系統時段由 `_job_slot_vars` 算（+3~+13 天）。

## 不要做的事

- 不要動 `.env` 提交（已 ignore，預設值在 `.env.example`）
- 不要對主倉 `worky` 做任何修改（這裡是測試框架，不修被測對象）
- 不要把 audit 帳號的 phone / password 寫在 commit message
- 不要 `git push upstream` 除非 user 明確要求（沿用主倉規範）

## 跨倉資訊查詢

需要查 endpoint 文件、Event/Handler 程式碼、推播 Type 常量時，直接讀
`/www/wwwroot/worky/` 下對應檔案：

- API 文件：`/www/wwwroot/worky/documents/api/<編號>-*.md`
- Event：`/www/wwwroot/worky/common/components/Contract/Event/After*.php`
- EventHandler：`/www/wwwroot/worky/common/components/Contract/EventHandler/After*/`
- PushNotification 子類：`/www/wwwroot/worky/common/components/Contract/EventHandler/After*/PushNotification.php`
- Type 常量：`/www/wwwroot/worky/common/components/PushNotification/Type.php`
- Consumer 屬性類：`/www/wwwroot/worky/common/components/RabbitMQ/Attribute/RabbitMQConsumer.php`
- OnEvent 屬性類：`/www/wwwroot/worky/common/components/EventTrigger/Attribute/OnEvent.php`
- ConfigLoader（自動掃描來源）：`/www/wwwroot/worky/common/helpers/ConfigLoader.php`
