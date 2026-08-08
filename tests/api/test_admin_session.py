import time

from app.api.admin_session import AdminSessionStore


def test_create_session_returns_a_token_that_verifies_true():
    store = AdminSessionStore()

    token = store.create_session()

    assert store.verify_session(token) is True


def test_verify_unknown_token_returns_false():
    store = AdminSessionStore()

    assert store.verify_session("not-a-real-token") is False


def test_verify_expired_token_returns_false():
    store = AdminSessionStore()
    token = store.create_session(ttl_seconds=-1)  # 立即过期

    assert store.verify_session(token) is False


def test_revoke_session_invalidates_the_token():
    store = AdminSessionStore()
    token = store.create_session()

    store.revoke_session(token)

    assert store.verify_session(token) is False
