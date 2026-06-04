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
from . import status as st

STATIC_DIR = Path(__file__).resolve().parent / "static"


@lru_cache(maxsize=1)
def _service() -> DashboardService:
    return DashboardService()


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
            else:
                self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._send_json({"error": str(e)}, 500)


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

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print("=" * 56)
    print("  Worky 承攬制任務看板  ")
    print(f"  DB     : {svc.settings.db_name} @ {svc.settings.db_host}")
    print(f"  開啟   : {url}")
    print("  停止   : Ctrl-C")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n看板已停止。")
    finally:
        httpd.server_close()
