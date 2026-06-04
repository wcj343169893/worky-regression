# Issue #4 — 用例清單預設只顯示主任務，子任務另頁下鑽（可遞迴）

## 背景
用例清單目前是平的一層。當一個高階用例被 AI 拆成「主任務 + 多個子任務」，甚至子任務
還能再拆，平鋪清單會非常雜亂。

## 需求
1. 用例清單**預設只展示主任務**（頂層用例）。
2. 主任務列上加一個按鈕（如「子任務」），點了**新開頁面**展示它的子任務清單。
3. 子任務若還有自己的子任務，也能在該頁再次下鑽查看（**遞迴**），並提供麵包屑回上層。

## 決策（已定）
1. **schema**：`qa_models.QACase` 增 `parent_id: str | None`（指向父用例 id，頂層為 NULL），
   加索引。改完跑 `alembic revision --autogenerate -m "qa_cases.parent_id"` →
   `alembic upgrade head`（**不要手寫 DDL**，遵守 CLAUDE.md）。
2. **list**：`cases.py` 的 `list_cases` 增 `parent_id` 參數：
   - 不傳 / 傳特殊值 `__root__` → 只回 `parent_id IS NULL` 的頂層用例（預設）。
   - 傳具體 id → 回該用例的直接子用例。
   每列附 `child_count`（子用例數），決定是否顯示「子任務」按鈕。
   YAML 來源的用例 `parent_id` 來自 spec 的 `parent` 欄位（無則頂層）；`sync_cases` 要寫入。
3. **API**：`/api/cases?parent_id=<id>` 回子清單；列回傳 `child_count`。
4. **前端**：`cases.js` 清單只渲染頂層；有子用例的列顯示「子任務（n）」按鈕，
   點擊 → 在用例頁內以「下鑽檢視 + 麵包屑」呈現該層子清單（同頁切換 view，
   或 hash 路由 `#cases/children/<id>`，可遞迴一路鑽下去 + 麵包屑回上層）。
5. **產生子用例**（前瞻相容）：AI 分解可在 spec 帶 `parent` 指定父用例；本 issue 至少要讓
   schema + 清單 + 下鑽頁完整可用（即使現有 generated 都還是頂層，UI 也不能壞）。

## 驗收
- 清單預設只看到頂層用例；有子用例者顯示「子任務(n)」按鈕。
- 點按鈕進子清單頁，麵包屑可逐層返回；子用例還有子用例時能繼續下鑽。
- 無子用例的用例不顯示按鈕；`alembic upgrade head` 乾淨；server 可 import、API 不 500。

## 涉及檔案
`src/worky_regression/qa_models.py`、`alembic/versions/*`(autogenerate)、
`dashboard/cases.py`、`dashboard/server.py`、`dashboard/static/cases.js`、`styles.css`、
`qa_store.py`(sync_cases 寫 parent_id / child_count 查詢)。
（在 Issue #2、#3 完成後做。）
