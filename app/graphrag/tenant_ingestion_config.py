from __future__ import annotations

import aiosqlite

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_ingestion_config (
    tenant_id      TEXT PRIMARY KEY,
    ingestion_mode TEXT NOT NULL DEFAULT 'extraction'
);
"""

_VALID_MODES = frozenset({"extraction", "etl"})


class InvalidIngestionModeError(Exception):
    """ingestion_mode 只能是 'extraction' 或 'etl' 之一。"""


async def ensure_ingestion_config_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


async def get_ingestion_mode(conn: aiosqlite.Connection, tenant_id: str) -> str:
    """未显式设置过的租户默认 'extraction'——这是现状（本次改造前唯一
    存在的数据接入方式），保证已有租户不需要任何额外配置就维持原行为。
    """
    cursor = await conn.execute(
        "SELECT ingestion_mode FROM tenant_ingestion_config WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row is not None else "extraction"


async def set_ingestion_mode(conn: aiosqlite.Connection, tenant_id: str, mode: str) -> None:
    if mode not in _VALID_MODES:
        raise InvalidIngestionModeError(
            f"不支持的接入模式: {mode!r}，仅支持: {sorted(_VALID_MODES)}"
        )
    await conn.execute(
        "INSERT INTO tenant_ingestion_config (tenant_id, ingestion_mode) VALUES (?, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET ingestion_mode = excluded.ingestion_mode",
        (tenant_id, mode),
    )
    await conn.commit()
