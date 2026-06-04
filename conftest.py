"""pytest 共用 fixture。"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

from worky_regression.config import Settings
from worky_regression.client import WorkyClient
from worky_regression.actor import Actor
from worky_regression.verifier import DBVerifier
from worky_regression.autotest import ensure_publisher_invoice


PROJECT_ROOT = Path(__file__).parent
ACCOUNTS_FILE = PROJECT_ROOT / "cases" / "_fixtures" / "test_accounts.yaml"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture(scope="session")
def accounts() -> dict:
    with ACCOUNTS_FILE.open() as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="session")
def db(settings: Settings) -> DBVerifier:
    return DBVerifier(settings)


@pytest.fixture(scope="session")
def publisher(settings: Settings, accounts: dict) -> Actor:
    """承攬制發案者：也是 Labor 角色（user_type=2）。

    Preflight：確保此 publisher 已設定發票資訊（type=0 捐贈發票，最少欄位），
    否則 /contract/task/publish 會 throw TASK_NO_INVOICE_SET_UP (50045)。
    """
    cfg = accounts["publisher_primary"]
    client = WorkyClient(settings, user_type=cfg["user_type"])
    actor = Actor(role="publisher", user_type=cfg["user_type"], phone=cfg["phone"],
                  user_id=cfg["id"], client=client)
    actor.login(audit_code=settings.audit_sms_code)
    ensure_publisher_invoice(actor)            # preflight：寫最小發票設定，避免 50045
    return actor


@pytest.fixture(scope="session")
def receiver(settings: Settings, accounts: dict) -> Actor:
    cfg = accounts["receiver_primary"]
    client = WorkyClient(settings, user_type=cfg["user_type"])
    actor = Actor(role="receiver", user_type=cfg["user_type"], phone=cfg["phone"],
                  user_id=cfg["id"], client=client)
    actor.login(audit_code=settings.audit_sms_code)
    return actor


# ===== 「工作」系統角色：雇主(user_type=1) + 打工夥伴(user_type=2) =====

@pytest.fixture(scope="session")
def employer(settings: Settings, accounts: dict) -> Actor:
    """工作系統的商家（user_type=1）。

    v31x 原本無雇主，此帳號由 scripts/bootstrap_job_env.py 建立（複製自 v30x）。
    若登入失敗，先跑：python scripts/bootstrap_job_env.py
    """
    cfg = accounts["employer_primary"]
    client = WorkyClient(settings, user_type=cfg["user_type"])
    actor = Actor(role="employer", user_type=cfg["user_type"], phone=cfg["phone"],
                  user_id=cfg["id"], client=client, shop_id=cfg.get("shop_id"))
    actor.login(audit_code=settings.audit_sms_code)
    return actor


@pytest.fixture(scope="session")
def labor(settings: Settings, accounts: dict) -> Actor:
    """工作系統的打工夥伴（user_type=2）。沿用既有 audit labor（publisher_primary）。"""
    cfg = accounts["publisher_primary"]
    client = WorkyClient(settings, user_type=cfg["user_type"])
    actor = Actor(role="labor", user_type=cfg["user_type"], phone=cfg["phone"],
                  user_id=cfg["id"], client=client)
    actor.login(audit_code=settings.audit_sms_code)
    return actor
