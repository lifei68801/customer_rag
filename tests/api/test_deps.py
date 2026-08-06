import logging

import pytest
from fastapi import HTTPException

from app.api import deps
from app.config.settings import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        gateway_shared_secret=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_get_gateway_tenant_id_returns_none_when_secret_not_configured():
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret=None, settings=_settings()
    )
    assert result is None


async def test_get_gateway_tenant_id_returns_header_value_when_secret_matches():
    settings = _settings(gateway_shared_secret="sekret")
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret="sekret", settings=settings
    )
    assert result == "t1"


async def test_get_gateway_tenant_id_rejects_when_secret_mismatches():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id="t1", x_gateway_secret="wrong", settings=settings
        )
    assert exc_info.value.status_code == 401


async def test_get_gateway_tenant_id_rejects_when_tenant_header_missing():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id=None, x_gateway_secret="sekret", settings=settings
        )
    assert exc_info.value.status_code == 401


def test_resolve_tenant_id_prefers_gateway_value():
    result = deps.resolve_tenant_id("t1", "t2", source="test")
    assert result == "t1"


def test_resolve_tenant_id_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        result = deps.resolve_tenant_id(None, "t2", source="test")
    assert result == "t2"
    assert any("t2" in record.message for record in caplog.records)


def test_resolve_tenant_id_raises_when_both_missing():
    with pytest.raises(HTTPException) as exc_info:
        deps.resolve_tenant_id(None, None, source="test")
    assert exc_info.value.status_code == 422
