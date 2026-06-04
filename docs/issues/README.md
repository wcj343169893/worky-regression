# Dashboard 改版 issues（2026-06）

由使用者需求拆成 5 個 issue，分群、依序由子進程逐個完成。

| # | 標題 | 群組 | 依賴 |
|---|------|------|------|
| [01](ISSUE-01-boards-from-executed-records.md) | 看板基於已執行記錄展示 | 看板 | — |
| [02](ISSUE-02-merge-case-menus.md) | 合併用例菜單為「測試用例」 | 用例 | — |
| [03](ISSUE-03-ai-decompose-tabs.md) | AI 分解加領域 tab | 用例 | #02 |
| [04](ISSUE-04-main-sub-cases.md) | 主任務 / 子任務下鑽 | 用例 | #02 #03 |
| [05](ISSUE-05-board-distribution-mismatch.md) | 看板分布/清單/分頁對齊 | 看板 | #01 |

兩群（看板 1/5、用例 2/3/4）共用 `server.py`，故**依序**執行，逐個提交。
執行順序：#02 → #03 → #04 → #01 → #05。
