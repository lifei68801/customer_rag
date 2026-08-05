from app.eval.dataset import EvalCase
from app.eval.runner import compare_planner_modes
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


async def test_compare_planner_modes_runs_both_graphs_and_returns_both_reports():
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
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "fake-llm",
        ScriptedLLMProvider(
            [
                # planner_disabled 路径：Responder 生成 -> OutputSafety 语义审查
                # -> faithfulness -> answer_relevancy
                "静态路径回答：重启路由器。",
                '{"is_safe": true}',
                '{"score": 0.9}',
                '{"score": 0.8}',
                # planner_enabled 路径：Planner 决策（直接回答，不调工具）
                # -> OutputSafety 语义审查 -> faithfulness -> answer_relevancy
                "Planner路径回答：重启路由器。",
                '{"is_safe": true}',
                '{"score": 0.7}',
                '{"score": 0.6}',
            ]
        ),
    )

    cases = [
        EvalCase(
            question="网络连不上怎么办？",
            expected_answer="重启路由器",
            expected_sources=["faq/network.md"],
        )
    ]

    report = await compare_planner_modes(
        cases,
        tenant_id="t1",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
    )

    assert report.planner_disabled.case_results[0].answer == "静态路径回答：重启路由器。"
    assert report.planner_enabled.case_results[0].answer == "Planner路径回答：重启路由器。"
    assert report.planner_disabled.average_faithfulness == 0.9
    assert report.planner_enabled.average_faithfulness == 0.7
