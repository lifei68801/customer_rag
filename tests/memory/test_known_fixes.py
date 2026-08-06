from datetime import datetime

import aiosqlite

from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes, register_known_fix
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


def _registry() -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FakeEmbeddingProvider())
    return registry


async def test_register_known_fix_persists_with_embedding():
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)

    fix_id = await register_known_fix(
        conn,
        tenant_id="t1",
        description="网关超时问题已修复",
        fixed_at=datetime(2026, 8, 1, 10, 0, 0),
        embedding_registry=_registry(),
        embedding_provider_name="fake-embedding",
    )

    assert fix_id

    fixes = await list_known_fixes(conn, tenant_id="t1")
    assert len(fixes) == 1
    assert fixes[0]["fix_id"] == fix_id
    assert fixes[0]["description"] == "网关超时问题已修复"
    assert fixes[0]["embedding"] == [1.0, 0.0]


async def test_list_known_fixes_scoped_to_tenant():
    conn = await aiosqlite.connect(":memory:")
    await ensure_known_fixes_schema(conn)
    await register_known_fix(
        conn, tenant_id="t1", description="修复A", fixed_at=datetime(2026, 8, 1),
        embedding_registry=_registry(), embedding_provider_name="fake-embedding",
    )

    fixes = await list_known_fixes(conn, tenant_id="t2")

    assert fixes == []
