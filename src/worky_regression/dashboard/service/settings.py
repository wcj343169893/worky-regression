"""系統設置（唯讀）：API / DeepSeek / 後台帳密。

#4：測試框架不再以被測 DB 為驗證目標，故設置頁不再顯示/查詢 worky 後端 DB
（移除「資料庫（驗證目標）」與「資料量」——後者是對 s_jobs 等的即時 COUNT 查詢）。
驗證結果改打對應 API 或走後台。本方法不再呼叫 self.db（開設置頁零後端 DB 查詢）。
"""
from __future__ import annotations


class SettingsMixin:
    def settings_info(self) -> dict:
        s = self.settings
        return {
            "qa_db_name": s.qa_db_name,                  # QA 看板自身的庫（非被測後端 DB）
            "api_base": s.api_base, "activity_api_base": s.activity_api_base,
            "platform": s.platform,
            "deepseek_model": s.deepseek_model, "deepseek_base_url": s.deepseek_base_url,
            "deepseek_key_set": bool(s.deepseek_api_key),
            # 後台管理員帳密（可編輯持久化；只回 password_set，不外洩明文）
            "backend": self.backend_config(),
        }
