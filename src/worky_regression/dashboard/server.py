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

import json
import mimetypes
import traceback
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .service import DashboardService
from .cases import CaseStore
from . import status as st

STATIC_DIR = Path(__file__).resolve().parent / "static"


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

    def _send_file(self, path: Path):
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8"
                         if ctype.startswith("text/") or ctype.endswith("javascript")
                         else ctype)
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
            # ── 測試用例 ──
            elif path == "/api/cases":
                self._send_json(_cases().list_cases(
                    system=_one(query, "system", "") or None,
                    q=_one(query, "q", ""),
                    limit=_int(query, "limit", 20),
                    offset=_int(query, "offset", 0),
                    parent_id=_one(query, "parent_id", "__root__")))
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
                    self._send_json(_cases().decompose_commit(
                        spec, run=bool((body or {}).get("run"))))
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止。")
    finally:
        httpd.server_close()
