# Issue #2 — 合併「工作/任務測試用例」為單一「測試用例」

## 背景
主菜單目前有「工作測試用例」「任務測試用例」兩個獨立入口 (`app.js` 的 `NAV`、
`cases.js` 的 `CASES`)。但框架要測的不只工作 / 任務，還有帳號註冊、審核、建立店鋪等，
分兩個固定菜單既佔位又不可擴充。

## 需求
主菜單把兩個用例入口合併為單一「**測試用例**」。原本用 `system`（job/contract）區分的
兩個頁面，改為一個頁面內以切換（系統 / 領域 tab，見 Issue #3）來篩選。

## 決策（已定）
1. `NAV` 移除 `job-cases` / `task-cases`，新增單一 `{ key: "cases", label: "測試用例" }`，
   放在「任務看板」之後。
2. `cases.js` 的 `CASES` 由「兩個固定 key」改為單一 `cases` 頁；`system` 不再寫死在路由，
   改為頁內狀態（預設 all/不限），供 Issue #3 的 tab 控制。
3. `/api/cases` 的 `system` 參數改為可選（不傳 = 全部）；`cases.py` 已支援 `system=None`。
4. 路由 fallback、active 高亮、`state` 鍵都改成 `cases`。

## 驗收
- 主菜單只剩一個「測試用例」入口，點進去能列出工作 + 任務所有用例。
- 舊 hash `#job-cases` / `#task-cases` 不致白屏（fallback 到 `#cases` 或 jobs 即可）。
- 前端無 console error。

## 涉及檔案
`src/worky_regression/dashboard/static/app.js`、`dashboard/static/cases.js`。
（與 Issue #3 緊耦合，按順序在 #2 之上做 #3。）
