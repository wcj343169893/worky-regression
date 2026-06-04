# Issue #5 — 任務看板「進度分布」與「清單 / 分頁」數據不對應

## 背景（bug）
任務看板左欄「進度分布」與右欄清單 / 分頁的數字對不上。根因：
- 進度分布 / 頂部統計走 `/api/stats`（`ContractMixin.stats`），對**整個** `s_contract_tasks`
  全表計算。
- 清單走 `/api/tasks`（`list_tasks`），有 `q` / 篩選 / **progress 是衍生值，在記憶體分頁後過濾**
  （`list_tasks` 先 `LIMIT/OFFSET` 再於記憶體 filter `progress`），導致 `total`（COUNT(*) 未含
  progress 過濾）與實際回傳列數、分布數字三方不一致。

## 需求
進度分布、頂部統計、清單 total、分頁，四者在「同一資料集合 + 同一套篩選」上保持一致。

## 決策（已定）
本 issue 與 Issue #1 同源：Issue #1 把看板資料集合改為「已執行記錄的 SN 集合」後，
stats / 分布 / 清單 / 分頁全部在該同一集合上計算即天然一致。
- 若 Issue #1 已落地：本 issue 只需**驗證**任務看板（及工作看板）三方數字一致，
  並修掉殘留的「衍生 progress 在分頁後過濾」造成的 total 與列數不符。
- progress 篩選必須在「分頁前」套用（先算出全集的衍生 progress → 過濾 → 再分頁 + COUNT），
  使 `total`、回傳列數、分布數字一致。

## 驗收
- 任意 progress 篩選下：分布該段數字 == 清單 total == 分頁總頁數推得的列數，一致。
- 切換篩選、翻頁，三方數字始終吻合。

## 涉及檔案
`dashboard/service/contract.py`（及 `jobs.py` 同類問題）、`dashboard/static/boards.js`。
（在 Issue #1 之後驗證 / 收尾。）

## 結案（2026-06-04，由 Issue #1 同源化解決）
Issue #1 已把 `list_tasks`/`stats`（及 `list_jobs`/`job_stats`）改為共用同一個
`_executed_*_rows()`，且 progress/category 篩選一律在「分頁前」套用 →
total / 回傳列數 / 分布段數字天然一致。無需額外程式改動，僅驗證：

- 任務看板：`list_tasks.total == stats.total`（=8）；`Σ by_progress.count == total`；
  每個 progress 段 `list_tasks(progress=code).total == seg.count` ✅
- 工作看板：`list_jobs.total == job_stats.total`（=50）；`Σ by_progress.count == total`；
  每個 category 段 `list_jobs(category=cat).total == seg.count`
  （matching=38 / recruited=11 / record_only=1，皆一致）✅
- 分頁：50 筆 = 20 + 20 + 10，三頁列數與 total 一致 ✅
