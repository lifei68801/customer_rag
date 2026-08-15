from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.tenant_ingestion_config import (
    InvalidIngestionModeError,
    ensure_ingestion_config_schema,
    get_ingestion_mode,
    set_ingestion_mode,
)

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ingestion_config_schema(conn)
    return conn


async def test_get_ingestion_mode_defaults_to_extraction():
    conn = await _conn()
    assert await get_ingestion_mode(conn, "unseen_tenant") == "extraction"


async def test_set_and_get_ingestion_mode():
    conn = await _conn()
    await set_ingestion_mode(conn, "muji", "etl")
    assert await get_ingestion_mode(conn, "muji") == "etl"
    # 未设置过的其它租户不受影响，仍是默认值
    assert await get_ingestion_mode(conn, "hotel_tenant") == "extraction"


async def test_set_ingestion_mode_rejects_invalid_value():
    conn = await _conn()
    with pytest.raises(InvalidIngestionModeError):
        await set_ingestion_mode(conn, "muji", "not_a_real_mode")


async def test_set_ingestion_mode_is_idempotent_overwrite():
    conn = await _conn()
    await set_ingestion_mode(conn, "muji", "etl")
    await set_ingestion_mode(conn, "muji", "extraction")
    assert await get_ingestion_mode(conn, "muji") == "extraction"
