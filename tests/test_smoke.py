"""Smoke test：驗證環境配置 + 兩個 audit 帳號能登入。"""
import pytest

from worky_regression.actor import Actor


@pytest.mark.smoke
def test_db_connectivity(db):
    """DB 可連，notifications 表存在。"""
    rows = db.query_all("SHOW TABLES LIKE 's_notifications'")
    assert rows, "s_notifications 表不存在或 DB 連不上"


@pytest.mark.smoke
def test_publisher_login(publisher: Actor):
    assert publisher.logged_in
    assert publisher.client.access_token, "publisher 沒拿到 accessToken"
    assert "|2|" in publisher.client.access_token, \
        f"承攬制發案者也是 labor，token 應含 |2|；實際 tail: ...{publisher.client.access_token[-30:]}"


@pytest.mark.smoke
def test_receiver_login(receiver: Actor):
    assert receiver.logged_in
    assert receiver.client.access_token, "receiver 沒拿到 accessToken"
    assert "|2|" in receiver.client.access_token, \
        f"token 應含 |2| 標記 labor，實際 token tail: ...{receiver.client.access_token[-30:]}"


@pytest.mark.smoke
def test_both_actors_authenticated(publisher: Actor, receiver: Actor):
    """兩個 actor 互不干擾、各自帶自己的 token。"""
    assert publisher.client.access_token != receiver.client.access_token
    assert publisher.user_id != receiver.user_id
