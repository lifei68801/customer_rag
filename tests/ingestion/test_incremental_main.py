import aiosqlite

from app.ingestion.incremental_main import main
from app.ingestion.ingestion_queue import ensure_ingestion_queue_schema, list_pending_jobs
from app.ingestion.tracking import ensure_tracking_schema, get_tracked_hash
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore
from tests.settings_factory import build_settings


def _settings():
    return build_settings()


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


async def test_main_scans_enqueues_and_processes_new_files(tmp_path):
    (tmp_path / "a.md").write_text("## 主题\n内容。\n", encoding="utf-8")

    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    summary = await main(
        directory=tmp_path,
        tenant_id="t1",
        settings=_settings(),
        ingestion_conn=conn,
        embedding_registry=embedding_registry,
        vector_store=vector_store,
    )

    assert summary["scanned"] == {"new": 1, "changed": 0, "deleted": 0, "unchanged": 0}
    assert summary["processed"] == 1
    assert await list_pending_jobs(conn) == []
    assert await get_tracked_hash(conn, tenant_id="t1", file_path=str(tmp_path / "a.md")) is not None


async def test_main_second_run_finds_nothing_new_to_do(tmp_path):
    (tmp_path / "a.md").write_text("## 主题\n内容。\n", encoding="utf-8")

    conn = await aiosqlite.connect(":memory:")
    await ensure_tracking_schema(conn)
    await ensure_ingestion_queue_schema(conn)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    await main(
        directory=tmp_path,
        tenant_id="t1",
        settings=_settings(),
        ingestion_conn=conn,
        embedding_registry=embedding_registry,
        vector_store=vector_store,
    )

    summary = await main(
        directory=tmp_path,
        tenant_id="t1",
        settings=_settings(),
        ingestion_conn=conn,
        embedding_registry=embedding_registry,
        vector_store=vector_store,
    )

    assert summary["scanned"]["unchanged"] == 1
    assert summary["processed"] == 0
