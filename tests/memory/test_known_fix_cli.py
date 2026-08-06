from datetime import datetime

import aiosqlite

from app.memory.known_fix_cli import cmd_list, cmd_register
from app.memory.known_fixes import ensure_known_fixes_schema
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


def _registry() -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FakeEmbeddingProvider())
    return registry


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)
    return conn


async def test_cmd_register_persists_and_returns_fix_id():
    conn = await _connect()

    fix_id = await cmd_register(
        tenant_id="t1",
        description="网关超时问题已修复",
        fixed_at=datetime(2026, 8, 5, 0, 0, 0),
        conn=conn,
        embedding_registry=_registry(),
        embedding_provider_name="fake-embedding",
    )

    assert fix_id

    fixes = await cmd_list(tenant_id="t1", conn=conn)
    assert len(fixes) == 1
    assert fixes[0]["fix_id"] == fix_id


async def test_cmd_list_returns_empty_when_no_fixes_registered():
    conn = await _connect()

    fixes = await cmd_list(tenant_id="t1", conn=conn)

    assert fixes == []
