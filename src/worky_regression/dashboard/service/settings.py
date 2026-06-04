"""系統設置（唯讀）：DB / API / DeepSeek 設定與資料量概覽。"""
from __future__ import annotations


class SettingsMixin:
    def settings_info(self) -> dict:
        s = self.settings
        return {
            "db_name": s.db_name, "db_host": s.db_host, "db_port": s.db_port,
            "qa_db_name": s.qa_db_name,
            "api_base": s.api_base, "platform": s.platform,
            "deepseek_model": s.deepseek_model, "deepseek_base_url": s.deepseek_base_url,
            "deepseek_key_set": bool(s.deepseek_api_key),
            "counts": {
                "jobs": self.db.query_one("SELECT COUNT(*) c FROM s_jobs WHERE is_deleted=0")["c"],
                "contract_tasks": self.db.query_one("SELECT COUNT(*) c FROM s_contract_tasks WHERE is_deleted=0")["c"],
                "labors": self.db.query_one("SELECT COUNT(*) c FROM s_labors")["c"],
                "employers": self.db.query_one("SELECT COUNT(*) c FROM s_employers")["c"],
                "shops": self.db.query_one("SELECT COUNT(*) c FROM s_shops")["c"],
            },
        }
