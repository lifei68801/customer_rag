import logging

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app


@pytest.fixture(autouse=True)
def isolate_startup_databases(monkeypatch, tmp_path):
    """把启动阶段会真实打开的两个 SQLite 库指向 tmp_path，并复位连接单例。

    这两个用例用 TestClient(app) 真实跑一遍 lifespan，而 lifespan 现在会在
    启动时回填租户注册表（见 app/main.py）——那一步要同时打开本体库和
    ingestion 库。不隔离的话，在一个还没有 data/ 目录的全新 clone 或 CI 上
    跑测试会顺手创建出这两个生产数据文件，还会把术语表按 terminology 种子
    文件建出来；测试套件不该有这种副作用。

    lifespan 自己 new 一个 Settings()（不走 get_settings 的 lru_cache），
    所以改环境变量就够，不需要注入。连接单例是模块级全局，用例前后各清一次，
    避免这里缓存的 tmp 连接泄漏给后面的测试、也避免前面的测试把真实库的
    连接留给这里。
    """
    monkeypatch.setenv(
        "CUSTOMER_RAG_GRAPH_REVIEW_DB_PATH", str(tmp_path / "ontology_store.sqlite3")
    )
    monkeypatch.setenv(
        "CUSTOMER_RAG_INGESTION_DB_PATH", str(tmp_path / "ingestion.sqlite3")
    )
    monkeypatch.setattr(deps, "_review_conn_cache", None)
    monkeypatch.setattr(deps, "_ingestion_conn_cache", None)
    yield
    monkeypatch.setattr(deps, "_review_conn_cache", None)
    monkeypatch.setattr(deps, "_ingestion_conn_cache", None)


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
