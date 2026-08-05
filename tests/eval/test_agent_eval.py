from app.agent.graph import build_agent_graph
from app.eval.dataset import EvalCase
from app.eval.runner import run_eval_suite_via_agent_graph
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


def _build_dependencies():
    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id="t1",
            metadata={},
        )
    ]
    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    return records, vector_store, bm25_index, embedding_registry


async def _seed(records, vector_store, bm25_index) -> None:
    await vector_store.upsert(records)
    bm25_index.index(records)


async def test_run_eval_suite_via_agent_graph_scores_static_path():
    records, vector_store, bm25_index, embedding_registry = _build_dependencies()
    await _seed(records, vector_store, bm25_index)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                "重启路由器即可解决。",  # Responder 生成答案
                '{"is_safe": true}',  # OutputSafety 语义审查
                '{"score": 0.9}',  # faithfulness
                '{"score": 0.8}',  # answer_relevancy
            ]
        ),
    )

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    cases = [
        EvalCase(
            question="网络连不上怎么办？",
            expected_answer="重启路由器",
            expected_sources=["faq/network.md"],
        )
    ]

    report = await run_eval_suite_via_agent_graph(
        cases,
        graph=graph,
        tenant_id="t1",
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
    )

    assert report.average_context_recall == 1.0
    assert report.average_faithfulness == 0.9
    assert report.average_answer_relevancy == 0.8
    assert len(report.case_results) == 1
    assert report.case_results[0].answer == "重启路由器即可解决。"


async def test_run_eval_suite_via_agent_graph_scores_zero_recall_on_fallback():
    """空知识库时 Agent graph 会走 Fallback（固定话术），context_recall 应为 0。"""
    _, vector_store, bm25_index, embedding_registry = _build_dependencies()
    # 故意不 seed 任何记录

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider([]))

    graph = build_agent_graph(
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    cases = [
        EvalCase(
            question="网络连不上怎么办？",
            expected_answer="重启路由器",
            expected_sources=["faq/network.md"],
        )
    ]

    report = await run_eval_suite_via_agent_graph(
        cases,
        graph=graph,
        tenant_id="t1",
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
    )

    assert report.average_context_recall == 0.0
