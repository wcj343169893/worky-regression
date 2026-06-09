#!/usr/bin/env python3
"""頁面標記（mark up）的 headless Claude 處理進程。

看板上使用者對某頁元素加的「標記」會以 status='pending' 落到 worky_qa_dashboard.qa_markups。
本進程獨立於看板 server 之外，輪詢 pending 標記，逐筆組 prompt 呼叫 `claude -p`（headless），
讓 Claude 依標記內容自動在本倉動手（改看板代碼 / 回覆建議），把處理摘要回寫 result，
狀態改 done / failed。

設計重點
--------
- **與看板 server 解耦**：server 只負責收標記，處理交給這個可獨立起停的 worker。
- **原子領取**：`claim_pending_markup()` 以 UPDATE ... WHERE status='pending' 搶占，多開幾個
  worker 也不會重領同一筆。
- **headless Claude**：`claude -p <prompt>` 非互動執行。要讓它能改檔需 `--dangerously-skip-permissions`
  （預設開；可用 --no-skip-permissions 關掉只讓它「回覆建議」不動檔）。
- **逐筆隔離**：每筆標記一個 claude 子行程，cwd=本倉根，逾時殺掉並記 failed。

用法
----
    source .venv/bin/activate
    python scripts/markup_worker.py                  # 持續輪詢（預設 5s）
    python scripts/markup_worker.py --once           # 只處理一筆就退出（除錯用）
    python scripts/markup_worker.py --interval 10     # 自訂輪詢秒數
    python scripts/markup_worker.py --no-skip-permissions   # 只回建議、不讓 Claude 動檔
    # 背景常駐：
    nohup python -u scripts/markup_worker.py > logs/markup_worker.log 2>&1 &
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

# 讓 `python scripts/markup_worker.py` 直接可 import 套件
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from worky_regression.config import Settings          # noqa: E402
from worky_regression.qa_store import QAStore          # noqa: E402

CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT = 1800  # 單筆標記給 Claude 的上限（秒）


def build_prompt(m: dict) -> str:
    """把一筆標記組成給 Claude 的指令。位置資訊讓它能定位到看板的哪個頁/元素。"""
    rect = m.get("rect") or {}
    lines = [
        "你正在維護「worky-regression」測試看板（純 stdlib HTTP server + 原生 JS 前端，",
        "前端在 src/worky_regression/dashboard/static/，後端在 src/worky_regression/dashboard/）。",
        "一位使用者在看板頁面上用「標記(mark up)」功能圈了一個元素並留下需求，請依需求動手處理。",
        "",
        "── 標記內容（使用者需求）──",
        (m.get("content") or "").strip(),
        "",
        "── 元素定位資訊 ──",
        f"頁面路由(hash)：#{m.get('route') or ''}",
        f"CSS 選擇器：{m.get('selector') or '(無)'}",
        f"元素可見文字：{(m.get('element_text') or '(無)')[:300]}",
        f"元素位置(px)：x={rect.get('x')} y={rect.get('y')} w={rect.get('w')} h={rect.get('h')} "
        f"視窗={rect.get('vw')}x{rect.get('vh')}",
    ]
    if m.get("screenshot_path"):
        lines += ["", f"當下截圖：{m['screenshot_path']}（相對本倉根，PNG）。如需視覺脈絡可讀取它。"]
    # 這是「再次優化」：曾處理過、使用者看了結果後追加回覆 → 帶上次結果 + 回覆串讓它迭代。
    replies = m.get("replies") or []
    if replies:
        if m.get("result"):
            lines += ["", "── 上一次的處理結果 ──", (m.get("result") or "").strip()[:4000]]
        lines += ["", "── 使用者對上次結果的追加回覆（由舊到新，請據此再次優化）──"]
        for i, rp in enumerate(replies, 1):
            lines.append(f"{i}. {(rp.get('text') or '').strip()}")
    lines += [
        "",
        "── 工作約束 ──",
        "- 路由 hash 對應的前端模組：jobs/tasks→boards.js、cases→cases.js、labors/employers/shops→tables.js、",
        "  accounts→accounts.js、markups→markup.js、settings→settings.js；共用元件在 widgets.js/util.js。",
        "- 遵守本倉 CLAUDE.md 與 README 的約束（繁中、不動 .env、不修被測主倉 worky）。",
        "- 改完請簡述你做了什麼、動了哪些檔；若判斷此標記只是提問/回饋，回覆建議即可，不必硬改檔。",
        "- 若需求不明確或風險過高，不要亂改，直接說明原因。",
    ]
    return "\n".join(lines)


def run_claude(prompt: str, *, skip_permissions: bool, timeout: int) -> tuple[bool, str]:
    """呼叫 headless claude，回 (ok, 輸出文字)。"""
    cmd = [CLAUDE_BIN, "-p", prompt]
    if skip_permissions:
        cmd.insert(1, "--dangerously-skip-permissions")
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"找不到 `{CLAUDE_BIN}` 執行檔，請確認 Claude Code CLI 已安裝且在 PATH。"
    except subprocess.TimeoutExpired:
        return False, f"claude 處理逾時（>{timeout}s），已中止。"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return False, f"claude 退出碼 {proc.returncode}\n{err or out}"
    return True, out or "(claude 無輸出)"


def process_one(qa: QAStore, *, skip_permissions: bool, timeout: int) -> bool:
    """領取並處理一筆 pending 標記；無待處理回 False。"""
    m = qa.claim_pending_markup()
    if not m:
        return False
    mid = m["id"]
    print(f"[markup-worker] 領取 #{mid}（#{m.get('route')}）：{(m.get('content') or '')[:60]}")
    ok, output = run_claude(build_prompt(m), skip_permissions=skip_permissions, timeout=timeout)
    qa.finish_markup(mid, status="done" if ok else "failed", result=output[:60000])
    print(f"[markup-worker] #{mid} → {'done' if ok else 'failed'}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="頁面標記的 headless Claude 處理進程")
    ap.add_argument("--once", action="store_true", help="只處理一筆就退出")
    ap.add_argument("--interval", type=float, default=5.0, help="無待處理時的輪詢秒數（預設 5）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="單筆 claude 上限秒數")
    ap.add_argument("--no-skip-permissions", dest="skip", action="store_false",
                    help="不加 --dangerously-skip-permissions（Claude 只回建議、不自動改檔）")
    ap.set_defaults(skip=True)
    args = ap.parse_args()

    settings = Settings.from_env()
    qa = QAStore(settings)
    qa.migrate()  # 確保 qa_markups 等表存在

    print(f"[markup-worker] 啟動。QA DB={settings.qa_db_name}@{settings.db_host} "
          f"skip_permissions={args.skip} interval={args.interval}s")
    if args.once:
        did = process_one(qa, skip_permissions=args.skip, timeout=args.timeout)
        if not did:
            print("[markup-worker] 無待處理標記。")
        return 0

    try:
        while True:
            try:
                did = process_one(qa, skip_permissions=args.skip, timeout=args.timeout)
            except Exception as e:  # noqa: BLE001 — 單筆失敗不該打掛輪詢
                print(f"[markup-worker] 處理出錯：{e}")
                did = False
            if not did:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[markup-worker] 已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
