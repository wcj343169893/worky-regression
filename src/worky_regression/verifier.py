"""DB-based 副作用驗證：查 s_notifications 表確認推播實際寫入。"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pymysql
import pymysql.cursors

from .config import Settings


@dataclass
class PushRecord:
    id: int
    type_id: int
    user_type: int
    uid: int
    title: str
    content: str
    click_uri: str
    created_at: int


class PushAssertionError(AssertionError):
    pass


class DBVerifier:
    """連線 worky DB 查推播記錄。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def _cursor(self):
        conn = pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_pass,
            database=self.settings.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                yield cur
        finally:
            conn.close()

    def max_notification_id(self) -> int:
        """取得目前 notifications 表的最大 id，作為後續 transition 的起點 watermark。"""
        with self._cursor() as cur:
            cur.execute("SELECT MAX(id) AS m FROM s_notifications")
            row = cur.fetchone()
            return int(row["m"] or 0)

    def pushes_after(self, watermark_id: int, *, uid: int, user_type: int,
                     timeout_sec: float = 5.0, poll_interval: float = 0.3
                     ) -> list[PushRecord]:
        """輪詢直到 watermark 後出現符合 uid/user_type 的推播，或 timeout。"""
        deadline = time.time() + timeout_sec
        while True:
            with self._cursor() as cur:
                cur.execute(
                    """
                    SELECT id, type_id, user_type, uid, title, content,
                           click_uri, created_at
                    FROM s_notifications
                    WHERE id > %s AND uid = %s AND user_type = %s
                    ORDER BY id ASC
                    """,
                    (watermark_id, uid, user_type),
                )
                rows = cur.fetchall()
            if rows or time.time() >= deadline:
                return [PushRecord(**r) for r in rows]
            time.sleep(poll_interval)

    def assert_push(self, watermark_id: int, *, recipient_uid: int,
                    recipient_user_type: int, expected_type_id: int,
                    title_contains: str | None = None,
                    body_contains: str | None = None) -> PushRecord:
        """驗證 transition 後對方收到符合 type_id 的推播。"""
        pushes = self.pushes_after(watermark_id, uid=recipient_uid,
                                   user_type=recipient_user_type)
        matches = [p for p in pushes if p.type_id == expected_type_id]
        if not matches:
            actual_types = [p.type_id for p in pushes]
            raise PushAssertionError(
                f"no push with type_id={expected_type_id} for uid={recipient_uid} "
                f"user_type={recipient_user_type} after id={watermark_id}; "
                f"got types={actual_types}"
            )
        push = matches[-1]
        if title_contains and title_contains not in push.title:
            raise PushAssertionError(
                f"title mismatch: expected to contain {title_contains!r}, got {push.title!r}"
            )
        if body_contains and body_contains not in push.content:
            raise PushAssertionError(
                f"body mismatch: expected to contain {body_contains!r}, got {push.content!r}"
            )
        return push

    def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """通用單列查詢（用於驗證業務表狀態）。"""
        with self._cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def flush_memcached(self, host: str = "127.0.0.1", port: int = 11211) -> bool:
        """Worky 用 memcached 做 model cache，DB UPDATE 後須 flush 否則讀到舊值。

        dev 環境直接 flush_all；如要更精確刪 key 可改成 delete <key>。
        """
        import socket
        try:
            with socket.create_connection((host, port), timeout=2) as s:
                s.send(b"flush_all\r\n")
                return s.recv(64).startswith(b"OK")
        except OSError:
            return False

    def execute(self, sql: str, params: tuple = ()) -> int:
        """UPDATE/INSERT/DELETE。回傳 affected rows。"""
        conn = pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_pass,
            database=self.settings.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                return cur.execute(sql, params)
        finally:
            conn.close()
