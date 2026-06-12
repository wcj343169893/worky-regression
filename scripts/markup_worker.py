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
import fcntl
import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# 讓 `python scripts/markup_worker.py` 直接可 import 套件
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from worky_regression.config import Settings          # noqa: E402
from worky_regression.qa_store import QAStore          # noqa: E402
from worky_regression.dashboard import gitops          # noqa: E402

CLAUDE_BIN = "claude"
DEFAULT_TIMEOUT = 1800  # 單筆標記給 Claude 的上限（秒）
LOCK_FILE = PROJECT_ROOT / "logs" / "markup_worker.lock"
DASHBOARD_URL = "http://127.0.0.1:8765"
FIX_PORT = 8766         # 緊急修復入口（獨立於看板，看板掛了也能呼叫）


def acquire_singleton_lock():
    """單例鎖：flock 不阻塞搶占；搶不到表示已有 worker 在跑（防代碼交叉修改），直接退出。

    回傳鎖檔 handle（須保持開啟，行程結束自動釋放）。
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "w")  # noqa: SIM115 — handle 要活到行程結束
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[markup-worker] 已有另一個 markup_worker 在跑（單例鎖被持有），本實例退出。")
        raise SystemExit(1)
    fh.write(f"{time.time()}\n")
    fh.flush()
    return fh


def build_prompt(m: dict) -> str:
    """把一筆標記組成給 Claude 的指令。依 kind 給不同視角：

    page（預設）—— 看板頁面元素標記：位置資訊讓它定位到哪個頁/元素，改看板前後端。
    feedback —— 用例執行失敗的意見反饋：content 內含用例關鍵信息，以「修復測試流程」
                視角處理（可改 cases/ YAML、cases/_specs/endpoints.yaml、框架代碼）。
    global —— 系統全局修改指令：無頁面定位，對本倉整體生效。
    """
    kind = m.get("kind") or "page"
    if kind == "feedback":
        lines = [
            "你正在維護「worky-regression」迴歸測試框架（用例 YAML 在 cases/，端點規格在",
            "cases/_specs/endpoints.yaml，框架代碼在 src/worky_regression/）。",
            "一位使用者對「某條測試用例的執行失敗」提交了意見反饋，內含用例關鍵信息與失敗現場。",
            "請依反饋修復測試流程：常見手段是修正用例 YAML（步驟/expect/guard）、校正 endpoints.yaml",
            "規格、或修框架選號/執行邏輯。注意：被測主倉 worky 的 bug 不要修，在結論中說明回報即可。",
            "",
            "── 意見反饋（含用例關鍵信息）──",
            (m.get("content") or "").strip(),
        ]
    elif kind == "global":
        lines = [
            "你正在維護「worky-regression」測試看板與迴歸框架（本倉根目錄）。",
            "一位使用者提交了「系統全局修改指令」（不針對特定頁面元素），請依指令對本倉動手處理。",
            "",
            "── 全局修改指令 ──",
            (m.get("content") or "").strip(),
        ]
    else:
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
    if kind == "page" and m.get("screenshot_path"):
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


def _parse_claude_json(stdout: str) -> tuple[str, dict]:
    """解析 `claude -p --output-format json` 的單一 result 物件，回 (結果文字, 成本資訊)。

    成本資訊：{tokens_in, tokens_out, cost_usd}。tokens_in 含 prompt 端全部（input +
    cache_creation + cache_read），tokens_out 為生成 token，cost_usd 取 CLI 權威的
    total_cost_usd。非 JSON（舊版 CLI / 髒輸出）時退回原文、成本歸零。
    """
    text = (stdout or "").strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return text, {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    u = obj.get("usage") or {}
    tokens_in = (int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                 + int(u.get("cache_read_input_tokens") or 0))
    cost = {
        "tokens_in": tokens_in,
        "tokens_out": int(u.get("output_tokens") or 0),
        "cost_usd": float(obj.get("total_cost_usd") or 0.0),
    }
    return (obj.get("result") or "").strip() or text, cost


def run_claude(prompt: str, *, skip_permissions: bool, timeout: int) -> tuple[bool, str, dict]:
    """呼叫 headless claude，回 (ok, 輸出文字, 成本資訊)。

    用 `--output-format json` 取得結構化結果，順帶拿到 token 消耗與 total_cost_usd。
    成本資訊 = {tokens_in, tokens_out, cost_usd}（失敗或非 JSON 時為 0）。

    降本旗標（每筆都是冷啟動的新會話，故省的是「固定底座」那 ~20k token）：
    - --exclude-dynamic-system-prompt-sections：把 cwd/git/env 移出 system prompt 到首則訊息。
      worker 會改檔、git status 每筆都變，移出後 system prompt 前綴保持穩定，下一筆更易命中
      快取 cache_read（訂閱認證 TTL 1h，相鄰標記 < 1h 即省下 cache_creation）。
    - --strict-mcp-config：不另給 --mcp-config 即零 MCP 載入，砍掉對「修本倉代碼」無用的
      MCP 工具 schema。
    刻意不加 --allowedTools：本 worker 修復任意使用者標記（可能 grep/改檔/跑測試/重啟服務），
    白名單漏列會讓正當修復失敗，風險大於省下的 token。如確定只動檔可自行加：
        cmd += ["--allowedTools", "Read,Edit,Write,Bash,Grep,Glob"]
    """
    zero = {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "json",
           "--exclude-dynamic-system-prompt-sections", "--strict-mcp-config"]
    if skip_permissions:
        cmd.insert(1, "--dangerously-skip-permissions")
    try:
        proc = subprocess.run(
            cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, f"找不到 `{CLAUDE_BIN}` 執行檔，請確認 Claude Code CLI 已安裝且在 PATH。", zero
    except subprocess.TimeoutExpired:
        return False, f"claude 處理逾時（>{timeout}s），已中止。", zero
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        # 失敗時 stdout 也可能是帶 cost 的 JSON（is_error=true），盡量把成本撈出來
        _, cost = _parse_claude_json(out)
        return False, f"claude 退出碼 {proc.returncode}\n{err or out}", cost
    result, cost = _parse_claude_json(out)
    return True, result or "(claude 無輸出)", cost


def process_one(qa: QAStore, *, skip_permissions: bool, timeout: int) -> bool:
    """領取並處理一筆 pending 標記；無待處理回 False。

    處理前後各拍一次 git 髒檔快照，差集＝本標記實際動到的檔案（files_changed），
    供「已解決→提交」「回滾」精準定位；同時記錄處理耗時 elapsed_ms。
    """
    m = qa.claim_pending_markup()
    if not m:
        return False
    mid = m["id"]
    print(f"[markup-worker] 領取 #{mid}（{m.get('kind') or 'page'}/#{m.get('route')}）："
          f"{(m.get('content') or '')[:60]}")
    before = gitops.dirty_files()
    t0 = time.monotonic()
    ok, output, cost = run_claude(build_prompt(m), skip_permissions=skip_permissions, timeout=timeout)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    changed = sorted(gitops.dirty_files() - before)
    qa.finish_markup(mid, status="done" if ok else "failed", result=output[:60000],
                     elapsed_ms=elapsed_ms, files_changed=changed,
                     tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
                     cost_usd=cost["cost_usd"])
    print(f"[markup-worker] #{mid} → {'done' if ok else 'failed'}（{elapsed_ms}ms，"
          f"改檔 {len(changed)} 個，token in/out {cost['tokens_in']}/{cost['tokens_out']}，"
          f"成本 ${cost['cost_usd']:.4f}）")
    # 改了檔就巡檢一次看板健康（worker 改壞看板代碼要及時發現並修復）。
    # 動到 src/ 下的 .py（看板後端 / 框架）必須重啟看板才生效——靜態 JS 即改即生效，
    # 但 Python 已載入進程，不重啟會出現「前端有按鈕、後端 404 not found」的半套狀態。
    if changed:
        if any(f.endswith(".py") and f.startswith("src/") for f in changed):
            print(f"[markup-worker] #{mid} 動到後端 .py，重啟看板使其生效")
            restart_dashboard()
        ensure_dashboard_healthy(auto_heal=True)
    return True


# ── 看板健康巡檢與緊急修復 ───────────────────────────────────────────────────
def dashboard_alive(timeout: float = 5.0) -> bool:
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/api/stats", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — 連不上/非200 都視為不健康
        return False


def restart_dashboard() -> None:
    """重啟看板：殺舊進程（精確匹配模組名）→ nohup 重新拉起 → 等待端口就緒。"""
    subprocess.run(["pkill", "-f", "worky_regression[.]dashboard"], check=False)
    time.sleep(1)
    subprocess.Popen(
        "nohup .venv/bin/python -u -m worky_regression.dashboard --host 0.0.0.0 --port 8765 "
        ">> logs/dashboard.log 2>&1 &",
        shell=True, cwd=str(PROJECT_ROOT))
    for _ in range(15):
        time.sleep(1)
        if dashboard_alive(2):
            return


def ensure_dashboard_healthy(auto_heal: bool = False) -> dict:
    """檢測看板是否正常；不正常且 auto_heal 時逐級修復，回傳各步驟記錄。

    修復階梯（每步後重測，好了就停）：
      ① 重啟看板（代碼可能改了但沒生效 / 進程死了）
      ② git stash 未提交變動（worker 改壞了還沒提交的代碼；stash 可救回不丟工作）再重啟
      ③ revert 最近一筆 fix(markup#…) commit（已提交的壞修改）再重啟
    """
    steps: list[str] = []
    if dashboard_alive():
        return {"healthy": True, "steps": ["服務正常"]}
    steps.append("健康檢查失敗")
    if not auto_heal:
        return {"healthy": False, "steps": steps}

    restart_dashboard()
    steps.append("① 已重啟看板")
    if dashboard_alive():
        return {"healthy": True, "steps": steps}

    r = subprocess.run(["git", "stash", "push", "-u", "-m", "emergency-fix 自動暫存"],
                       cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    steps.append(f"② git stash 未提交變動：{(r.stdout or r.stderr).strip()[:120]}")
    restart_dashboard()
    steps.append("② 已重啟看板")
    if dashboard_alive():
        return {"healthy": True, "steps": steps}

    log = subprocess.run(["git", "log", "-5", "--pretty=%H %s"], cwd=str(PROJECT_ROOT),
                         capture_output=True, text=True).stdout
    target = next((ln.split()[0] for ln in log.splitlines() if "fix(markup#" in ln), None)
    if target:
        r = subprocess.run(["git", "revert", "--no-edit", target], cwd=str(PROJECT_ROOT),
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "revert", "--abort"], cwd=str(PROJECT_ROOT), check=False)
            steps.append(f"③ revert {target[:10]} 衝突，已中止")
        else:
            steps.append(f"③ 已 revert 最近的標記 commit {target[:10]}")
            restart_dashboard()
            steps.append("③ 已重啟看板")
            if dashboard_alive():
                return {"healthy": True, "steps": steps}
    else:
        steps.append("③ 找不到 fix(markup#…) commit 可 revert")
    steps.append("✗ 自動修復未能恢復服務，需人工介入")
    return {"healthy": False, "steps": steps}


class _FixHandler(BaseHTTPRequestHandler):
    """緊急修復入口（GET /fix 觸發檢測+修復；GET /health 只檢測）。

    跑在 worker 行程內、與看板完全解耦——看板代碼被改壞起不來時，這個入口仍然可用。
    """

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] not in ("/fix", "/health"):
            self.send_response(404); self.end_headers()
            return
        result = ensure_dashboard_healthy(auto_heal=self.path.startswith("/fix"))
        body = json.dumps(result, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200 if result.get("healthy") else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        print(f"[markup-worker] 緊急修復入口 {self.path} → {result}")

    def log_message(self, *_):  # 安靜，避免刷爆 worker log
        pass


def start_fix_server() -> None:
    """背景執行緒起緊急修復 HTTP 入口（0.0.0.0:8766）。端口被占（如另一實例殘留）則略過。"""
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", FIX_PORT), _FixHandler)
    except OSError as e:
        print(f"[markup-worker] 緊急修復入口端口 {FIX_PORT} 不可用，略過：{e}")
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[markup-worker] 緊急修復入口：GET http://<host>:{FIX_PORT}/fix（/health 只檢測）")


def main() -> int:
    ap = argparse.ArgumentParser(description="頁面標記的 headless Claude 處理進程")
    ap.add_argument("--once", action="store_true", help="只處理一筆就退出")
    ap.add_argument("--interval", type=float, default=5.0, help="無待處理時的輪詢秒數（預設 5）")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="單筆 claude 上限秒數")
    ap.add_argument("--no-skip-permissions", dest="skip", action="store_false",
                    help="不加 --dangerously-skip-permissions（Claude 只回建議、不自動改檔）")
    ap.set_defaults(skip=True)
    args = ap.parse_args()

    _lock = acquire_singleton_lock()  # noqa: F841 — handle 須存活到行程結束（持鎖）

    settings = Settings.from_env()
    qa = QAStore(settings)
    qa.migrate()  # 確保 qa_markups 等表存在

    print(f"[markup-worker] 啟動（單例鎖已持有）。QA DB={settings.qa_db_name}@{settings.db_host} "
          f"skip_permissions={args.skip} interval={args.interval}s")
    if not args.once:
        start_fix_server()
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
