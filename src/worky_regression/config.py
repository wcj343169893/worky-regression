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
    # 商家端可走獨立 base/secret（如 /qa-v1：QA 模式帶 shop_id 鎖店）；打工端仍走主 base
    # （qa-v1 配 SOURCE_WEB，labor 的 APP 限定端點會被來源白名單擋掉，故分流）。
    # 未設則沿用 api_base / api_secret。注意 /qa-v1 模組有專屬 apiSecret，base 與 secret 要成對。
    employer_api_base: str
    employer_api_secret: str
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
            employer_api_base=os.environ.get("WORKY_EMPLOYER_API_BASE", api_base).rstrip("/"),
            employer_api_secret=os.environ.get("WORKY_EMPLOYER_API_SECRET",
                                               os.environ.get("WORKY_API_SECRET", "")),
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

    def api_base_for(self, user_type: int) -> str:
        """該 user_type 的主 API base：1=employer 可分流（如 /qa-v1），2=labor 走 api_base。"""
        return self.employer_api_base if user_type == 1 else self.api_base

    def api_secret_for(self, user_type: int) -> str:
        """與 api_base_for 成對的 apiSecret（/qa-v1 模組有專屬 secret）。"""
        return self.employer_api_secret if user_type == 1 else self.api_secret

    def for_system(self, system: str) -> "Settings":
        """回傳該系統要連的 DB 設定：contract 走 contract_db_name（若有設），其餘沿用。"""
        if system == "contract" and self.contract_db_name:
            return replace(self, db_name=self.contract_db_name)
        return self


# ── 被測倉分支 → 庫名（防 .env 與實際分支漂移）─────────────────────────────────
# 不同分支對應不同被測庫（切分支＝換一套測試數據）。這裡由被測倉 git 分支推算「預期庫名」，
# 與 .env 的 WORKY_DB_NAME 比對，不一致即告警——正是先前 v30x/v31x 漂移踩到的坑。
# 被測倉路徑可用 WORKY_SRC_DIR 覆寫（預設 /www/wwwroot/worky，與 CLAUDE.md 一致）。
WORKY_SRC_DIR = os.environ.get("WORKY_SRC_DIR", "/www/wwwroot/worky")


def worky_branch(worky_dir: str = WORKY_SRC_DIR) -> str:
    """讀被測倉當前分支（優先 git-branch.txt，否則 git rev-parse），小寫；讀不到回空字串。"""
    import subprocess
    f = Path(worky_dir) / "git-branch.txt"
    if f.exists():
        b = f.read_text(encoding="utf-8", errors="ignore").strip()
        if b:
            return b.lower()
    try:
        out = subprocess.run(
            ["git", "--git-dir", f"{worky_dir}/.git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return out.stdout.strip().lower()
    except Exception:  # noqa: BLE001 — 環境無 git / 路徑不存在 → 視為讀不到
        return ""


def expected_worky_db(branch: str) -> str:
    """由分支名推算被測庫名（移植 worky/common/config/main-local.php）。非 next 分支回預設。"""
    database = "worky_next_v221x"  # main-local.php 的預設值
    if branch.startswith("next"):
        names = ["worky", "next"]
        parts = branch.split("-")
        if "fix" in parts or "wkd" in parts:
            names.append("staging")
        version = parts[1] if len(parts) > 1 else ""
        suffix = parts[2] if len(parts) > 2 else ""
        if version and suffix in ("plus",):
            version = f"{version}_{suffix}"
        if version:
            names.append(version)
        database = "_".join(names)
    return database


def db_consistency(settings: "Settings", worky_dir: str = WORKY_SRC_DIR) -> dict:
    """回傳被測倉分支 / 推算庫 / .env 庫 / 是否一致，供看板顯示與啟動告警。"""
    br = worky_branch(worky_dir)
    exp = expected_worky_db(br) if br else ""
    return {
        "branch": br,
        "expected_db": exp,
        "configured_db": settings.db_name,
        "consistent": bool(br) and exp == settings.db_name,
        "worky_dir": worky_dir,
    }
