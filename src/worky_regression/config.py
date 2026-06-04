"""環境配置（.env 載入）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_base: str
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

    platform: str
    sdk_version: str
    device_name: str

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

        return cls(
            api_base=req("WORKY_API_BASE").rstrip("/"),
            api_secret=req("WORKY_API_SECRET"),
            audit_sms_code=req("WORKY_AUDIT_SMS_CODE"),
            db_host=req("WORKY_DB_HOST"),
            db_port=int(os.environ.get("WORKY_DB_PORT", "3306")),
            db_user=req("WORKY_DB_USER"),
            db_pass=req("WORKY_DB_PASS"),
            db_name=req("WORKY_DB_NAME"),
            contract_db_name=os.environ.get("WORKY_CONTRACT_DB_NAME", ""),
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
