import logging

from fastapi.testclient import TestClient

from app.main import app


def test_startup_warns_when_gateway_shared_secret_unset(monkeypatch, caplog):
    monkeypatch.delenv("CUSTOMER_RAG_GATEWAY_SHARED_SECRET", raising=False)
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            pass

    assert any(
        "gateway_shared_secret" in record.message for record in caplog.records
    )


def test_startup_does_not_warn_when_gateway_shared_secret_set(monkeypatch, caplog):
    monkeypatch.setenv("CUSTOMER_RAG_GATEWAY_SHARED_SECRET", "sekret")
    with caplog.at_level(logging.WARNING):
        with TestClient(app):
            pass

    assert not any(
        "gateway_shared_secret" in record.message for record in caplog.records
    )
