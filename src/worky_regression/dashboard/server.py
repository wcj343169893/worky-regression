"""看板 HTTP server（純 stdlib，無額外依賴）。

路由：
  GET /                     → SPA 首頁
  GET /static/<file>        → 靜態資源
  GET /api/meta             → enum 對照 + 進度定義
  GET /api/stats            → 頂部統計
  GET /api/tasks            → 任務清單（?q= &progress= &publisher_id= &limit= &offset=）
  GET /api/tasks/<task_sn>  → 任務詳情
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import secrets
import time
import traceback
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .service import DashboardService
from .cases import CaseStore
from . import status as st

STATIC_DIR = Path(__file__).resolve().parent / "static"
# results/markups/：標記截圖落地處（results 已 gitignore）
MARKUP_SHOT_DIR = Path(__file__).resolve().parents[3] / "results" / "markups"

# ── 前端資源 cache-busting ───────────────────────────────────────────────────
# 看板前端是原生 ES Module（import 走相對路徑、無版本號）。no-store 只能管到瀏覽器，
# 管不住「無視 no-store 的邊緣代理 / CDN」——它快取住某個時點的 .js 後，之後改檔強刷也拉不到新版
# （常見的「修了沒生效」）。解法：服務 .js/.html 時，動態把所有靜態資源 URL 補上隨檔案 mtime 變動的
# ?v=<ver>，任一檔案一變動版本就換、URL 就換，任何快取都被迫重抓。檔案查找只看 path、忽略 query。
_JS_IMPORT_RE = re.compile(r'(\b(?:from|import)\b\s*\(?\s*)(["\'])(\.{1,2}/[^"\']+\.js)(["\'])')
_HTML_ASSET_RE = re.compile(r'((?:src|href)=)(["\'])(/static/[^"\']+\.(?:js|css))(["\'])')


def _static_build_version() -> str:
    """所有前端原始檔（js/css/html）的最新 mtime → 短版本字串；任一檔改動即變動。"""
    latest = 0
    for p in STATIC_DIR.rglob("*"):
        if p.suffix in (".js", ".css", ".html") and p.is_file():
            latest = max(latest, int(p.stat().st_mtime))
    return format(latest, "x")


def _bust_js_imports(src: str, ver: str) -> str:
    """把 JS 內相對 import（from/import "./x.js"）的 URL 補上 ?v=ver。"""
    return _JS_IMPORT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}?v={ver}{m.group(4)}", src)


def _bust_html_assets(src: str, ver: str) -> str:
    """把 HTML 內 /static/*.js|css 的 src/href 補上 ?v=ver。"""
    return _HTML_ASSET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}?v={ver}{m.group(4)}", src)


@lru_cache(maxsize=1)
def _service() -> DashboardService:
    return DashboardService()


@lru_cache(maxsize=1)
def _cases() -> CaseStore:
    return CaseStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkyDashboard/1.0"

    # 安靜一點的 log
    def log_message(self, fmt, *args):  # noqa: N802
        print(f"  [http] {self.address_string()} {fmt % args}")

    def _send_json(self, payload, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse_start(self):
        """開一條 Server-Sent Events 串流連線（標頭只送一次，之後逐筆 _sse_send）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        # 關掉 nginx 等反代的緩衝，event 才會即時到前端
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse_send(self, etype: str, payload):
        """送一筆 SSE 事件；前端關頁造成的 BrokenPipeError 由呼叫端吞掉以續跑。"""
        data = json.dumps({"type": etype, **payload}, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_file(self, path: Path):
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        # JS/HTML：把資源 URL 補上版本號（破代理快取，見 _static_build_version 註解）
        suffix = path.suffix.lower()
        if suffix in (".js", ".html"):
            ver = _static_build_version()
            text_data = data.decode("utf-8")
            text_data = _bust_js_imports(text_data, ver) if suffix == ".js" else _bust_html_assets(text_data, ver)
            data = text_data.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or ctype.endswith("javascript")
                         else ctype)
        # 看板是內部測試工具、改版頻繁；ES Module / HTML 不帶版本號，瀏覽器一旦
        # 快取舊檔就會看到「修了卻沒生效」的假象（每次都得手動硬重整）。一律禁快取。
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self._send_file(STATIC_DIR / "index.html")
            elif path.startswith("/static/"):
                # 防目錄穿越
                rel = path[len("/static/"):]
                target = (STATIC_DIR / rel).resolve()
                if STATIC_DIR.resolve() in target.parents or target == STATIC_DIR.resolve():
                    self._send_file(target)
                else:
                    self._send_json({"error": "forbidden"}, 403)
            elif path == "/api/meta":
                self._send_json(st.meta_payload())
            elif path == "/api/stats":
                self._send_json(_service().stats())
            elif path == "/api/tasks":
                self._send_json(_service().list_tasks(
                    q=_one(query, "q", ""),
                    progress=_int_or_none(query, "progress"),
                    publisher_id=_int_or_none(query, "publisher_id"),
                    filters=_filters(query),
                    limit=_int(query, "limit", 50),
                    offset=_int(query, "offset", 0),
                ))
            elif path.startswith("/api/tasks/"):
                sn = path[len("/api/tasks/"):]
                detail = _service().task_detail(sn)
                if detail is None:
                    self._send_json({"error": f"task_sn {sn} not found"}, 404)
                else:
                    self._send_json(detail)
            # ── 工作系統 ──
            elif path == "/api/job-stats":
                self._send_json(_service().job_stats())
            elif path == "/api/jobs":
                self._send_json(_service().list_jobs(
                    q=_one(query, "q", ""),
                    category=_one(query, "category", "") or None,
                    filters=_filters(query),
                    limit=_int(query, "limit", 50), offset=_int(query, "offset", 0)))
            elif path.startswith("/api/jobs/"):
                sn = path[len("/api/jobs/"):]
                detail = _service().job_detail(sn)
                if detail is None:
                    self._send_json({"error": f"job_sn {sn} not found"}, 404)
                else:
                    self._send_json(detail)
            # ── 管理 ──
            elif path == "/api/labors":
                self._send_json(_service().list_labors(
                    q=_one(query, "q", ""), filters=_filters(query),
                    limit=_int(query, "limit", 50), offset=_int(query, "offset", 0)))
            elif path == "/api/employers":
                self._send_json(_service().list_employers(
                    q=_one(query, "q", ""), filters=_filters(query),
                    limit=_int(query, "limit", 50), offset=_int(query, "offset", 0)))
            elif path == "/api/shops":
                self._send_json(_service().list_shops(
                    q=_one(query, "q", ""), filters=_filters(query),
                    limit=_int(query, "limit", 50), offset=_int(query, "offset", 0)))
            elif path == "/api/settings":
                self._send_json(_service().settings_info())
            elif path == "/api/accounts":
                # 帳號池管理頁：檢視 labor/employer 池（qa_accounts，非被測後端 DB）
                self._send_json(_service().list_accounts())
            # ── 頁面標記（mark up）──
            elif path == "/api/markups":
                _qa = _cases().qa
                _st = _one(query, "status", "") or None
                _q = _one(query, "q", "") or None
                self._send_json({
                    "items": _qa.list_markups(
                        status=_st, q=_q,
                        limit=_int(query, "limit", 100),
                        offset=_int(query, "offset", 0)),
                    "total": _qa.count_markups(status=_st, q=_q)})
            elif path.startswith("/api/markups/") and path.endswith("/screenshot"):
                mid = path[len("/api/markups/"):-len("/screenshot")]
                self._send_markup_shot(mid)
            # ── 測試用例 ──
            elif path == "/api/cases":
                self._send_json(_cases().list_cases(
                    system=_one(query, "system", "") or None,
                    q=_one(query, "q", ""),
                    limit=_int(query, "limit", 20),
                    offset=_int(query, "offset", 0),
                    parent_id=_one(query, "parent_id", "__root__")))
            elif path == "/api/cases/run-stream":
                # SSE 即時逐步執行：連線本身驅動執行，每步開始/結束推一筆事件給看板。
                # EventSource 只能 GET，故 id 走 query string（?id=<case_id>）。
                cid = _one(query, "id", "")
                if not cid:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._run_stream(cid)
            elif path.startswith("/api/cases/") and path.endswith("/steps"):
                cid = path[len("/api/cases/"):-len("/steps")]
                data = _cases().case_steps(cid)
                if data is None:
                    self._send_json({"error": f"case {cid} not found"}, 404)
                else:
                    self._send_json(data)
            elif path.startswith("/api/cases/"):
                cid = path[len("/api/cases/"):]
                detail = _cases().case_detail(cid)
                if detail is None:
                    self._send_json({"error": f"case {cid} not found"}, 404)
                else:
                    self._send_json(detail)
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _run_stream(self, case_id: str):
        """以 SSE 跑一條用例：連線本身驅動執行，逐步推送事件給看板。

        執行同步跑在這條請求自己的 thread 內（ThreadingHTTPServer），邊跑邊吐 event。
        前端中途關頁 → 寫入觸發 BrokenPipeError，標記斷線後續事件不再嘗試寫，
        但 run_case_streaming 仍會跑完並照常落地 worky_qa_dashboard（關頁不丟 run）。
        """
        self._sse_start()
        disconnected = {"v": False}

        def on_event(etype, payload):
            if disconnected["v"]:
                return
            try:
                self._sse_send(etype, payload)
            except (BrokenPipeError, ConnectionResetError):
                disconnected["v"] = True

        try:
            _cases().run_case_streaming(case_id, on_event)
        except Exception as e:  # noqa: BLE001 — 已開 SSE，錯誤改以事件送出
            traceback.print_exc()
            if not disconnected["v"]:
                try:
                    self._sse_send("error", {"error": str(e)})
                except (BrokenPipeError, ConnectionResetError):
                    pass

    def _client_ip(self) -> str:
        """建立者來源 IP：經邊緣代理時取 X-Forwarded-For 第一跳，否則用 socket 對端。"""
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
        return str(self.client_address[0])[:64]

    def _create_markup(self, body: dict) -> dict:
        """落地一筆標記：截圖 dataURL 解碼存檔，其餘欄位入庫。

        kind：page=頁面元素標記（預設）；feedback=用例失敗意見反饋；global=系統全局修改指令。
        """
        content = str(body.get("content", "")).strip()
        if not content:
            return {"error": "缺少標記內容 content"}
        kind = str(body.get("kind", "page"))
        if kind not in ("page", "feedback", "global"):
            kind = "page"
        shot_rel = self._save_markup_shot(body.get("screenshot"))
        ts = int(time.time())
        mid = _cases().qa.insert_markup(
            route=str(body.get("route", ""))[:64],
            selector=(str(body.get("selector"))[:4000] if body.get("selector") else None),
            element_text=(str(body.get("element_text"))[:2000] if body.get("element_text") else None),
            rect=body.get("rect") if isinstance(body.get("rect"), dict) else None,
            content=content[:8000],
            screenshot_path=shot_rel,
            created_at=ts, kind=kind, ip=self._client_ip())
        return {"ok": True, "id": mid}

    @staticmethod
    def _commit_markup_changes(mid: int) -> dict:
        """「已解決」時把該標記動到的檔案提交成獨立 commit；無變動/失敗不擋解決，回警告。"""
        from . import gitops
        m = _cases().qa.get_markup(mid) or {}
        files = m.get("files_changed") or []
        if m.get("commit_sha") or m.get("rolled_back") or not files:
            return {}
        # 只提交「目前仍有變動」的檔案——之後的標記可能又動過同檔，已被提交/還原者跳過
        pending = [f for f in files if f in gitops.dirty_files()]
        if not pending:
            return {"commit_warning": "該標記記錄的檔案目前無未提交變動（可能已被提交或還原）"}
        try:
            sha = gitops.commit_markup(mid, pending, (m.get("content") or "").replace("\n", " "))
            _cases().qa.set_markup_commit(mid, sha)
            return {"commit_sha": sha, "committed_files": pending}
        except Exception as e:  # noqa: BLE001 — 提交失敗不擋「已解決」，但要讓使用者知道
            return {"commit_warning": f"提交失敗：{e}"}

    @staticmethod
    def _rollback_markup(mid: int) -> dict:
        """回滾某標記的修改（revert 已提交 / 還原未提交），並回寫 rolled_back 標記。"""
        from . import gitops
        m = _cases().qa.get_markup(mid)
        if not m:
            return {"error": f"找不到標記 #{mid}"}
        if m.get("rolled_back"):
            return {"error": "此標記已回滾過"}
        try:
            note = gitops.rollback_markup(
                commit_sha=m.get("commit_sha"), files=m.get("files_changed") or [])
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        _cases().qa.set_markup_rolled_back(mid, note)
        return {"ok": True, "id": mid, "note": note}

    @staticmethod
    def _save_markup_shot(data_url) -> str | None:
        """把 base64 dataURL 截圖寫到 results/markups/，回傳相對路徑；無 / 解析失敗回 None。"""
        if not data_url or not isinstance(data_url, str) or "," not in data_url:
            return None
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1])
        except (ValueError, TypeError):
            return None
        MARKUP_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        name = f"markup-{int(time.time())}-{secrets.token_hex(3)}.png"
        (MARKUP_SHOT_DIR / name).write_bytes(raw)
        return f"results/markups/{name}"

    def _send_markup_shot(self, markup_id: str):
        """回傳某標記的截圖檔（依庫中相對路徑解析，防穿越）。"""
        try:
            m = _cases().qa.get_markup(int(markup_id))
        except (ValueError, TypeError):
            m = None
        rel = (m or {}).get("screenshot_path")
        if not rel:
            self._send_json({"error": "no screenshot"}, 404)
            return
        target = (MARKUP_SHOT_DIR.parents[1] / rel).resolve()
        if MARKUP_SHOT_DIR.resolve() not in target.parents:
            self._send_json({"error": "forbidden"}, 403)
            return
        self._send_file(target)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            body = self._read_json()
            if path == "/api/cases/run":
                cid = (body or {}).get("id")
                if not cid:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(_cases().run_case(cid))
            elif path == "/api/cases/copy":
                # 以既有用例 spec 為範本快速再建一條新用例（新 id、新檔，不含執行歷史）
                cid = (body or {}).get("id")
                if not cid:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(_cases().copy_case(cid))
            elif path == "/api/cases/republish":
                # 重新發佈：以該用例 spec 為範本複製成新 id 後立即執行（時間綁定用例每次都落成獨立新記錄）
                cid = (body or {}).get("id")
                if not cid:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(_cases().republish_case(cid))
            elif path in ("/api/cases/analyze", "/api/cases/swap-account"):
                # analyze：失敗步驟的 AI 診斷（只回建議，不自動執行）
                # swap-account：排除失敗 actor 目前帳號，配池中另一個同能力號整支重跑
                cid = (body or {}).get("id")
                si = _step_index(body)
                if not cid or si is None:
                    self._send_json({"error": "缺少 id / step_index"}, 400)
                elif path.endswith("/analyze"):
                    self._send_json(_cases().analyze_failure(cid, si))
                else:
                    self._send_json(_cases().swap_account(cid, si))
            elif path == "/api/cases/tab":
                # 依自然語言描述，AI 產生一個分解 tab 設定（label/system/query/placeholder）
                desc = str((body or {}).get("description", "")).strip()
                if not desc:
                    self._send_json({"error": "缺少 description"}, 400)
                else:
                    self._send_json(_cases().suggest_tab(desc))
            elif path == "/api/cases/decompose/preview":
                # 分解第一段：呼叫 LLM 產 plan/spec 但不落地（不寫檔、不入庫），回前端彈窗確認
                uc = str((body or {}).get("use_case", "")).strip()
                if not uc:
                    self._send_json({"error": "缺少 use_case"}, 400)
                else:
                    # system 為前端 tab 指定的目標系統（可選；空 = 讓 LLM 自己判斷）
                    sysname = str((body or {}).get("system", "")).strip() or None
                    self._send_json(_cases().decompose_preview(uc, system=sysname))
            elif path == "/api/cases/decompose/commit":
                # 分解第二段：使用者確認/校正後才真正落地（寫檔 + sync_cases，run 才執行）
                spec = (body or {}).get("spec_yaml") or (body or {}).get("spec")
                if not spec:
                    self._send_json({"error": "缺少 spec_yaml / spec"}, 400)
                else:
                    # children（可選）：前端送「使用者勾選保留」的子用例 spec_yaml 陣列，
                    # 透傳給 decompose_commit 一併落地（綁 parent=主最終 id，不執行）
                    children = (body or {}).get("children") or None
                    self._send_json(_cases().decompose_commit(
                        spec, run=bool((body or {}).get("run")), children=children))
            elif path == "/api/cases/decompose":
                # 舊一步到位路由：保留向後相容（CLI / 既有呼叫端）
                uc = str((body or {}).get("use_case", "")).strip()
                if not uc:
                    self._send_json({"error": "缺少 use_case"}, 400)
                else:
                    # system 為前端 tab 指定的目標系統（可選；空 = 讓 LLM 自己判斷）
                    sysname = str((body or {}).get("system", "")).strip() or None
                    self._send_json(_cases().decompose(
                        uc, run=bool((body or {}).get("run")), system=sysname))
            # ── 系統設置：後台管理員帳密 + 審核 ──
            elif path == "/api/settings/backend":
                b = body or {}
                self._send_json(_service().update_backend_config(
                    base=b.get("base"), username=b.get("username"),
                    password=b.get("password")))
            elif path == "/api/backend/login-test":
                self._send_json(_service().backend_login_test())
            elif path == "/api/accounts/test-login":
                # 帳號池：以該帳號實打被測登入 API，回 ok/訊息（診斷登入報錯）
                aid = (body or {}).get("account_id")
                role = (body or {}).get("role")
                if aid is None or not role:
                    self._send_json({"error": "缺少 account_id / role"}, 400)
                else:
                    self._send_json(_service().account_test_login(int(aid), str(role)))
            elif path == "/api/accounts/state":
                # 帳號池：啟用 / 停用（disabled 者 acquire 不配發）
                aid = (body or {}).get("account_id")
                role = (body or {}).get("role")
                state = (body or {}).get("state")
                if aid is None or not role or not state:
                    self._send_json({"error": "缺少 account_id / role / state"}, 400)
                else:
                    self._send_json(_service().set_account_state(int(aid), str(role), str(state)))
            elif path == "/api/accounts/register":
                # 帳號池：純 API 自助建帳號入池（產 09 手機號 → 註冊 → 補資料）
                role = (body or {}).get("role")
                n = (body or {}).get("n", 1)
                # caps＝目標能力（指定時依此決定步驟與是否核准）；auto_review 僅在未指定 caps 時生效
                caps = (body or {}).get("caps")
                caps = [str(x) for x in caps] if isinstance(caps, list) and caps else None
                auto_review = bool((body or {}).get("auto_review", True))
                if not role:
                    self._send_json({"error": "缺少 role"}, 400)
                else:
                    self._send_json(_service().register_accounts(
                        str(role), int(n), caps=caps, auto_review=auto_review))
            elif path == "/api/accounts/init":
                # 全清重建帳號池：按能力分群各建 per_cap 個（耗時較長，同步）
                per_cap = (body or {}).get("per_cap", 3)
                self._send_json(_service().init_pool(per_cap=int(per_cap)))
            # ── 頁面標記（mark up）──
            elif path == "/api/markups":
                self._send_json(self._create_markup(body or {}))
            elif path == "/api/markups/delete":
                mid = _review_id(body)  # 共用：取 body.id 為 int
                if mid is None:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    _cases().qa.delete_markup(mid)
                    self._send_json({"ok": True})
            elif path == "/api/markups/reply":
                mid = _review_id(body)
                reply = str((body or {}).get("content", "")).strip()
                if mid is None or not reply:
                    self._send_json({"error": "缺少 id 或回覆內容"}, 400)
                elif _cases().qa.reply_markup(mid, reply[:8000], at=int(time.time())):
                    self._send_json({"ok": True, "id": mid})  # 已打回 pending，待 worker 再次處理
                else:
                    self._send_json({"error": f"找不到標記 #{mid}"}, 404)
            elif path == "/api/markups/resolve":
                # 已解決開關：resolved=1 → 源頁面不再畫框 + 把 worker 動到的檔案提交成
                # 獨立 commit（sha 回寫，供回滾 revert）；0 → 重新顯示（不撤 commit，回滾另有按鈕）
                mid = _review_id(body)
                resolved = bool((body or {}).get("resolved", True))
                if mid is None:
                    self._send_json({"error": "缺少 id"}, 400)
                elif _cases().qa.set_markup_resolved(mid, resolved):
                    out = {"ok": True, "id": mid, "resolved": resolved}
                    if resolved:
                        out.update(self._commit_markup_changes(mid))
                    self._send_json(out)
                else:
                    self._send_json({"error": f"找不到標記 #{mid}"}, 404)
            elif path == "/api/markups/rollback":
                # 撤銷某標記的代碼修改：已提交 → git revert；未提交 → 還原工作區檔案
                mid = _review_id(body)
                if mid is None:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(self._rollback_markup(mid))
            elif path == "/api/labors/review":
                rid = _review_id(body)
                if rid is None:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(_service().review_labor(
                        rid, approve=bool((body or {}).get("approve")),
                        reasons=(body or {}).get("reasons")))
            elif path == "/api/shops/review":
                rid = _review_id(body)
                if rid is None:
                    self._send_json({"error": "缺少 id"}, 400)
                else:
                    self._send_json(_service().review_shop(
                        rid, approve=bool((body or {}).get("approve")),
                        reason_ids=(body or {}).get("reason_ids"),
                        other_reason=str((body or {}).get("other_reason", ""))))
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}


def _one(q: dict, key: str, default: str = "") -> str:
    return q.get(key, [default])[0]


def _int(q: dict, key: str, default: int) -> int:
    try:
        return int(q.get(key, [default])[0])
    except (ValueError, TypeError):
        return default


_RESERVED = {"q", "limit", "offset", "category", "progress", "publisher_id"}


def _filters(q: dict) -> dict:
    """收集非保留的 query 參數當篩選條件（service 端再以白名單過濾）。"""
    return {k: v[0] for k, v in q.items() if k not in _RESERVED and v and v[0] != ""}


def _step_index(body: dict):
    """從 POST body 取 step_index（int），缺失/非數字回 None。"""
    v = (body or {}).get("step_index")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _review_id(body: dict):
    """從 POST body 取審核對象 id（int），缺失/非數字回 None。"""
    v = (body or {}).get("id")
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(q: dict, key: str):
    v = q.get(key, [None])[0]
    if v in (None, ""):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    # 啟動前先驗證 DB 連得上，早點失敗
    svc = _service()
    svc.db.max_notification_id()
    # 確保 QA 看板庫 schema 到最新（alembic upgrade head；建庫 + 建/改表）
    _cases().qa.migrate()

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print("=" * 56)
    print("  Worky 承攬制任務看板  ")
    print(f"  DB     : {svc.settings.db_name} @ {svc.settings.db_host}")
    print(f"  QA DB  : {svc.settings.qa_db_name}")
    print(f"  開啟   : {url}")
    print("  停止   : Ctrl-C")
    print("=" * 56)
    # 被測倉分支 ↔ .env 庫名一致性告警（不同分支對應不同庫；漂移會讓清單/審核對不上）
    from ..config import db_consistency
    _dc = db_consistency(svc.settings)
    if not _dc["consistent"]:
        print(f"  ⚠️  被測庫不一致：分支 {_dc['branch'] or '?'} 推算庫={_dc['expected_db'] or '?'} "
              f"≠ .env={_dc['configured_db']}")
        print("      切分支＝換一套測試數據：請改 .env WORKY_DB_NAME 並重建帳號池，或切回對應分支。")
        print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止。")
    finally:
        httpd.server_close()
