"""pytest 共用 fixture。"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path

from worky_regression.config import Settings
from worky_regression.client import WorkyClient
from worky_regression.actor import Actor
from worky_regression.verifier import DBVerifier


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
    _ensure_publisher_invoice(actor)
    return actor


def _ensure_publisher_invoice(actor: Actor) -> None:
    """以 audit publisher 身份呼叫 /contract/invoice/update 寫入最小發票設定。

    Idempotent：每 session 跑一次，覆寫舊值不會壞事。
    """
    resp = actor.client.post(
        "/contract/invoice/update",
        body={
            "type": 0,                          # 捐贈發票
            "name": "regression",
            "phone": actor.phone,
            "email": "regression@worky.local",
            "e_invoice_carrier_type": 0,        # 無載具（捐贈用）
            "mobile_carrier_number": "",
            "citizen_carrier_number": "",
            "tax_id_number": "",
            "tax_id_number_title": "",
        },
    )
    if resp.status_code != 200 or resp.json().get("success") is False:
        raise RuntimeError(
            f"failed to setup invoice for publisher id={actor.user_id}: {resp.text[:300]}"
        )


@pytest.fixture(scope="session")
def receiver(settings: Settings, accounts: dict) -> Actor:
    cfg = accounts["receiver_primary"]
    client = WorkyClient(settings, user_type=cfg["user_type"])
    actor = Actor(role="receiver", user_type=cfg["user_type"], phone=cfg["phone"],
                  user_id=cfg["id"], client=client)
    actor.login(audit_code=settings.audit_sms_code)
    return actor
