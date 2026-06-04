# CLAUDE.md — worky-regression（給 AI 的工作約束）

> 本檔**只放對 AI 的約束與建議**。專案定位、核心觀念、架構、程式碼結構、執行方式、
> QA 持久化設計、跨倉資訊查詢、已知陷阱的完整背景，請見 **`README.md`**。

所有回答使用**繁體中文**。

## 工作慣例

- **不要刪 audit 帳號的綁卡 / 發票**（手工建的，重建很麻煩）：
  `s_pay_user_invoice_info`、`s_contract_pay_fun_point_credit_cards`（publisher 236）。
- **YAML path 步驟順序很重要**，狀態機不能跳關。
- 加新 transition / 改 enum / 改 push type_id：**只動 `cases/_specs/endpoints.yaml`**
  （`transitions.py` / `push_type_ids.py` 已是 shim，不要再往裡塞資料）。
- endpoint 路徑以 `/www/wwwroot/worky/documents/api/` 為準；
  push type_id 以 `/www/wwwroot/worky/common/components/PushNotification/Type.php` 為準。
- **QA 看板 schema（worky_qa_dashboard）改動**：只改 `qa_models.py` 的 SQLAlchemy 模型，
  再 `alembic revision --autogenerate -m "..."` → `alembic upgrade head`；**不要手寫 DDL**。
- `qa_store` 的 raw SQL 對 `system` 欄一律加反引號 `` `system` ``（MySQL 8.0 保留字）。
- **測試帳號要按「能力」拿，不要在執行期 SQL 挖工作庫**：用 `AccountPool.acquire(role, caps, n)`
  （`qa_accounts.py`）。新增/校正帳號走 `provision()`（特權、偶發），不在用例裡臨時改帳號硬狀態。
  加多夥伴用例：`bind: {labor: laborN}` 切身份、spec 頂層 `vars:` 帶參數、`assert_state:` 驗負向。
- 後台用 `nohup` 起時加 `python -u`（否則 banner / log 被緩衝看不到）。
- commit message 格式：`<type>: <描述>(WKD-XXXXX)`（沿用 worky 慣例；有對應 Jira 單再帶號）。

## 已知陷阱（會影響你怎麼動手；背景見 README）

- **dev 環境 contract / job 分庫**：改庫**只動 `.env`**（`WORKY_DB_NAME` / `WORKY_CONTRACT_DB_NAME`），
  別寫死在程式裡。`Settings.for_system()` 依 path 系統自動選庫。
- **被測對象的 bug 不要在框架側修**（例：v31x `J2 labor-apply` 壞、PHP-FPM static 污染），
  回報主倉即可——這裡是測試框架，不修被測對象。
- **PHP-FPM static 污染**導致 T2 立刻找不到 T1 剛建的 task（`50010`）：
  請 user 跑 `sudo systemctl restart php8.2-fpm`，框架無法在 Python 端清掉。
- contract 付款用 **ATM `payment_method_id=3`**（v31x 已無 FunPoint 綁卡）；
  happy path 用 `db_exec` 把 `pay_status` 改 `102` 繞過。
- receiver `T6→T7` < 1s 會回 `9002`，happy path 間插 `- sleep: 2`。
- `memcached flush_all` 清不掉 PHP `static`，runner 預設只在 `db_exec`（`flush_cache: true`）才 flush。

## 不要做的事

- 不要動 `.env` 提交（已 ignore，預設值在 `.env.example`）。
- 不要對主倉 `worky` 做任何修改（這裡是測試框架，不修被測對象）。
- 不要把 audit 帳號的 phone / password 寫在 commit message。
- 不要 `git push upstream` 除非 user 明確要求（沿用主倉規範）。
