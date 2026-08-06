import aiosqlite

from app.memory.consolidation_queue import (
    enqueue_consolidation_job,
    list_pending_jobs,
    mark_job_completed,
    mark_job_failed,
    process_pending_jobs,
)
from app.memory.memory_store import list_active_memory_items
from app.memory.schema import ensure_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_enqueue_creates_a_pending_job():
    conn = await _connect()

    job_id = await enqueue_consolidation_job(
        conn,
        tenant_id="t1",
        user_id="u1",
        session_id="s1",
        user_input="我们公司用的是企业版套餐",
        assistant_output="好的，已记录",
    )

    pending = await list_pending_jobs(conn)
    assert len(pending) == 1
    assert pending[0]["job_id"] == job_id
    assert pending[0]["status"] == "pending"
    assert pending[0]["attempts"] == 0


async def test_enqueue_is_idempotent_for_the_same_turn():
    conn = await _connect()

    job_id_1 = await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="相同输入", assistant_output="相同输出",
    )
    job_id_2 = await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="相同输入", assistant_output="相同输出",
    )

    assert job_id_1 == job_id_2
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1


async def test_mark_job_completed_removes_it_from_pending():
    conn = await _connect()
    job_id = await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="x", assistant_output="y",
    )

    await mark_job_completed(conn, job_id)

    assert await list_pending_jobs(conn) == []


async def test_mark_job_failed_retries_until_max_attempts_then_goes_dead():
    conn = await _connect()
    job_id = await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="x", assistant_output="y",
    )

    await mark_job_failed(conn, job_id, error="第一次失败", max_attempts=3)
    pending = await list_pending_jobs(conn)
    assert len(pending) == 1  # 还没到上限，退回 pending 供重试
    assert pending[0]["attempts"] == 1

    await mark_job_failed(conn, job_id, error="第二次失败", max_attempts=3)
    await mark_job_failed(conn, job_id, error="第三次失败", max_attempts=3)

    assert await list_pending_jobs(conn) == []  # 达到上限，转入死信，不再被捞到


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


async def test_process_pending_jobs_runs_consolidation_and_marks_completed():
    conn = await _connect()
    await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="我们公司用的是企业版套餐", assistant_output="好的，已记录",
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # detect_delay_intent
                '{"facts": ["客户使用企业版套餐"]}',
                '{"actions": [{"event": "ADD", "target_memory_id": "", '
                '"text": "客户使用企业版套餐", "reason": "首次提及"}]}',
            ]
        ),
    )

    processed = await process_pending_jobs(
        conn, llm_registry=llm_registry, llm_provider_name="llm"
    )

    assert processed == 1
    assert await list_pending_jobs(conn) == []
    items = await list_active_memory_items(conn, tenant_id="t1", user_id="u1")
    assert [i["text"] for i in items] == ["客户使用企业版套餐"]


class ExplodingEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        raise RuntimeError("embedding provider 挂了")


async def test_process_pending_jobs_marks_failed_job_for_retry_without_crashing_batch():
    conn = await _connect()
    await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s1",
        user_input="我们公司用的是企业版套餐", assistant_output="好的，已记录",
    )
    await enqueue_consolidation_job(
        conn, tenant_id="t1", user_id="u1", session_id="s2",
        user_input="第二轮问题", assistant_output="第二轮回答",
    )

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        ScriptedLLMProvider(
            [
                '{"is_delay": false}',  # job1 的 detect_delay_intent
                '{"facts": ["事实1"]}',
                '{"is_delay": false}',  # job2 的 detect_delay_intent
                '{"facts": ["事实2"]}',
            ]
        ),
    )
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", ExplodingEmbeddingProvider())

    processed = await process_pending_jobs(
        conn,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
    )

    assert processed == 0
    pending = await list_pending_jobs(conn)
    assert len(pending) == 2
    assert all(job["attempts"] == 1 for job in pending)
