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
| `src/worky_regression/transitions.py` | 10 個 `Transition`（endpoint / event / push_type / body_template） |
| `src/worky_regression/push_type_ids.py` | PushNotification Type 常量 → 數字 |
| `src/worky_regression/verifier.py` | `DBVerifier`：watermark、`assert_push`、`execute`、`flush_memcached` |
| `src/worky_regression/runner.py` | `PathRunner` — 讀 YAML，展開 `{{state.xxx}}` / `{{publisher.user_id}}` |
| `conftest.py` | session fixtures：`settings/db/publisher/receiver`；publisher 自動寫發票 |
| `cases/path-*.yaml` | 一個檔 = 一條 path |
| `cases/_fixtures/test_accounts.yaml` | audit 帳號 |

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
- 加新 transition / type_id 時，**同步更新** `transitions.py` 與 `push_type_ids.py`
- endpoint 路徑以 `/www/wwwroot/worky/documents/api/` 為準
- push type_id 以 `/www/wwwroot/worky/common/components/PushNotification/Type.php` 為準
- 提交時 commit message 格式：`<type>: <描述>(WKD-XXXXX)`（沿用 worky 慣例；若該變動有對應 Jira 單再帶）

## 已知陷阱（重要！）

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

### ATM 付款

`payment_method_id=3` 會在 T6 卡住等付款。框架統一改用 `payment_method_id=1`（FunPoint
信用卡，publisher 236 已預先綁定），但 dev 環境不會真扣款，path 中要靠 `db_exec`：

```yaml
- db_exec: "UPDATE s_contract_tasks SET pay_status=102 WHERE task_sn='{{state.task_sn}}'"
  flush_cache: true
```

### 時段條件

- `start_time >= now + 720s`（dev `MIN_PUBLISH_INTERVAL_SECONDS = 0.2h`）
- `end_time - start_time >= 3600s`

runner 自動設 `start = now + 900`, `end = now + 4600`，YAML 不用自己算。

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
