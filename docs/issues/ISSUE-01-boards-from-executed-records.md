# Issue #1 — 看板改為「基於本系統已執行記錄」展示

## 背景
目前「工作看板 / 任務看板」(`service/jobs.py`、`service/contract.py`) 直接全表掃描主倉
工作庫 (`s_jobs` / `s_contract_tasks`)，把整個 dev 工作庫的資料都倒出來。這不是回歸測試
框架該關心的範圍——看板應該只反映「**本框架實際跑過、產生 / 觸碰過的工作與任務**」。

## 需求
看板（工作 + 任務）的清單、頂部統計、進度分布，都只基於本系統已執行過的記錄
（`worky_qa_dashboard` 的 `qa_runs` / `qa_run_steps`），**不再以直連工作庫全表掃描為資料來源**。

## 決策（已定）
1. **行集合來源**：從 `qa_run_steps.observations.saved` 抽出每次執行保存的實體序號
   （`task_sn` / `job_sn`）；以這些 SN 的集合作為看板清單的「全集」。
2. **現況豐富化**：對工作庫的查詢一律以 `WHERE task_sn IN (...)` / `WHERE job_sn IN (...)`
   縮限到上述 SN 集合，只用來補當前 status/pay_status/進度。工作庫不再是「row set 的來源」，
   只是「已執行記錄」這批 SN 的現況讀取。
   - 若 saved 內缺進度欄位、且工作庫已查不到該 SN，仍要能列出該筆（以最後一次執行的
     run 狀態 / 時間呈現），不可整列消失。
3. **統計 / 分布同源**：頂部統計與「進度分布」必須在「步驟 1 的同一 SN 集合」上計算，
   與清單、分頁完全一致（這同時修掉 Issue #5）。
4. 加一個 `QAStore` 方法回傳「已執行實體」：`executed_entities(system) -> [{sn, last_run_id,
   last_status, last_started_at, runs}]`，供 service 層 join。
5. 看板每列可選擇性顯示「最近執行結果 / 執行次數」，把看板綁回測試框架（與用例頁一致風格）。

## 驗收
- 任務看板、工作看板只顯示本框架跑過的 task/job。
- 進度分布長條 + 圖例數字 == 清單 total == 分頁總數（三者一致）。
- 工作庫查詢都帶 SN 白名單，無全表掃描。
- `python -c "import worky_regression.dashboard.server"` 可 import；看板能起、API 不 500。

## 涉及檔案
`src/worky_regression/qa_store.py`、`dashboard/service/contract.py`、`dashboard/service/jobs.py`、
`dashboard/server.py`(若需新參數)、`dashboard/static/boards.js`。
