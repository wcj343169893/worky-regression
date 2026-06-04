# Issue #3 — AI 用例分解加領域 tab（工作 / 任務 / 打工夥伴 / 商家…）

## 背景
AI 用例分解目前只有一個 textarea，`system` 靠 `_detect_system` 從 transition 前綴猜。
不同領域（工作流程、承攬任務流程、打工夥伴帳號生命週期、商家 / 店鋪）關注點不同，
分解時給 LLM 的提示與目標 system 應該更有針對性。

## 需求
在「測試用例」頁的「AI 用例分解」面板加多個 tab：**工作 / 任務 / 打工夥伴 / 商家**（可擴充），
選定 tab 後分解更有針對性（決定 `system` 與領域提示），且同時作為下方用例清單的篩選。

## 決策（已定）
1. tab 定義成一份可擴充的設定（label / key / system / placeholder / 領域提示語）。
   - 工作 → system=job；任務 → system=contract；打工夥伴 / 商家 → 帳號生命週期類
     （目前 planner 只支援 job/contract，這兩個 tab 先帶領域提示並標示「規劃中」，
     不可讓 decompose 直接 500——若 planner 不支援該 system，前端友善提示）。
2. 切 tab 同時：①切換 AI 分解的目標 system + placeholder；②過濾下方用例清單。
3. `/api/cases/decompose` 增加可選 `system` / `domain` 參數帶給 planner（planner 端
   優先採用前端指定的 system，而非只靠猜）。
4. tab 樣式沿用既有 `.pill` / `.nav` 視覺語彙，加進 `styles.css`。

## 驗收
- 面板上方有 4 個（含未來可加）tab，預設選「工作」或「全部」。
- 切 tab → 清單即時過濾 + 分解目標 system 切換。
- 在「工作」「任務」tab 分解可正常產生 generated/*.yaml；不支援的 tab 給明確提示不 500。

## 涉及檔案
`src/worky_regression/dashboard/static/cases.js`、`dashboard/static/styles.css`、
`dashboard/server.py`（decompose 收 system 參數）、`planner.py`（採用指定 system）、
`dashboard/cases.py`（decompose 透傳）。
（在 Issue #2 完成後做。）
