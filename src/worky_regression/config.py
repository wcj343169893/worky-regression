"""環境配置（.env 載入）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_base: str
    # 營運活動 Activity API base（/activity，與主 API 的 /v1 不同 base）。
    # 預設由 api_base 的 host 推導；可用 WORKY_ACTIVITY_API_BASE 覆寫。
    activity_api_base: str
    api_secret: str
    audit_sms_code: str

    db_host: str
    db_port: int
    db_user: str
    db_pass: str
    db_name: str
    # 承攬制資料所在 DB：dev 環境 contract 與 job 分庫（job 在 db_name=worky_next_v31x，
    # contract 由 API 寫到 worky_next_staging_v30x）。空字串→沿用 db_name。
    contract_db_name: str
    # QA 看板資料庫：用例註冊 + 每次執行結果（與 worky 庫同 server，共用 host/port/user/pass）。
    qa_db_name: str

    platform: str
    sdk_version: str
    device_name: str

    # 後台管理員（backend.*.worky.com.tw）預設 URL；帳密不放 .env，改由看板 UI 編輯
    # 並持久化到 qa_settings（見 qa_models.QASetting）。此處只給 base 的 .env 預設值。
    backend_base: str

    # DeepSeek API（用例分解器 Layer ③；OpenAI 相容介面。無 key 時分解器停用，其餘框架照常）
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        if env_file is None:
            env_file = Path(__file__).resolve().parents[2] / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        def req(key: str) -> str:
            val = os.environ.get(key)
            if not val:
                raise RuntimeError(f"missing required env: {key}")
            return val

        api_base = req("WORKY_API_BASE").rstrip("/")
        # 預設 Activity base：取 api_base 的 scheme://host，接 /activity（與 /v1 同 host 不同前綴）
        from urllib.parse import urlsplit
        _p = urlsplit(api_base)
        default_activity = (f"{_p.scheme}://{_p.netloc}/activity"
                            if _p.scheme and _p.netloc else api_base + "/activity")

        return cls(
            api_base=api_base,
            activity_api_base=os.environ.get("WORKY_ACTIVITY_API_BASE", default_activity).rstrip("/"),
            api_secret=req("WORKY_API_SECRET"),
            audit_sms_code=req("WORKY_AUDIT_SMS_CODE"),
            db_host=req("WORKY_DB_HOST"),
            db_port=int(os.environ.get("WORKY_DB_PORT", "3306")),
            db_user=req("WORKY_DB_USER"),
            db_pass=req("WORKY_DB_PASS"),
            db_name=req("WORKY_DB_NAME"),
            contract_db_name=os.environ.get("WORKY_CONTRACT_DB_NAME", ""),
            qa_db_name=os.environ.get("WORKY_QA_DB_NAME", "worky_qa_dashboard"),
            backend_base=os.environ.get("WORKY_BACKEND_BASE", "").rstrip("/"),
            platform=os.environ.get("WORKY_PLATFORM", "WebPC"),
            sdk_version=os.environ.get("WORKY_SDK_VERSION", "1.0.0"),
            device_name=os.environ.get("WORKY_DEVICE_NAME", "regression-runner"),
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        )

    def for_system(self, system: str) -> "Settings":
        """回傳該系統要連的 DB 設定：contract 走 contract_db_name（若有設），其餘沿用。"""
        if system == "contract" and self.contract_db_name:
            return replace(self, db_name=self.contract_db_name)
        return self
