import aiosqlite

from app.memory.consolidation_queue import enqueue_consolidation_job, list_pending_jobs
from app.memory.consolidation_worker import main
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from tests.settings_factory import build_settings


def _settings():
    return build_settings()


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


async def test_main_processes_pending_jobs_using_injected_dependencies():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await enqueue_consolidation_job(
        conn,
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "qwen",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": ["客户使用企业版套餐"]}',
                '{"actions": [{"event": "ADD", "target_memory_id": "", '
                '"text": "客户使用企业版套餐", "reason": "首次提及"}]}',
            ]
        ),
    )
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("qwen-embedding", FakeEmbeddingProvider())

    processed = await main(
        settings=_settings(),
        memory_conn=conn,
        llm_registry=llm_registry,
        embedding_registry=embedding_registry,
    )

    assert processed == 1
    assert await list_pending_jobs(conn) == []
