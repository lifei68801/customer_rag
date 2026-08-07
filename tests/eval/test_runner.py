from app.eval.dataset import EvalCase
from app.eval.runner import run_eval_suite
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    """依次返回：回答生成、语义安全审查、faithfulness打分、answer_relevancy打分。

    answer_question() 内部会对生成的回答调用一次 semantic_safety_review()
    （见 app/qa/answer.py），这也是走同一个 llm_registry 的一次 LLM 请求，
    夹在"回答生成"和 run_eval_suite() 自己发起的两次裁判打分请求之间，
    脚本响应列表必须按这个真实顺序排列，否则会错位消费。
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


async def test_run_eval_suite_aggregates_all_metrics_across_cases():
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
                "重启路由器即可解决。",  # 回答生成
                '{"is_safe": true}',  # 语义安全审查（answer_question 内部触发）
                '{"score": 0.9}',  # faithfulness
                '{"score": 0.8}',  # answer_relevancy
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

    report = await run_eval_suite(
        cases,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tenant_id="t1",
        query_rewrite_enabled=False,
    )

    assert report.average_context_recall == 1.0
    assert report.average_faithfulness == 0.9
    assert report.average_answer_relevancy == 0.8
    assert len(report.case_results) == 1
    assert report.case_results[0].question == "网络连不上怎么办？"


async def test_run_eval_suite_scores_zero_recall_when_tenant_id_does_not_match():
    """确认 eval runner 自身也遵守租户隔离——评错租户不该拿到别的租户的资料。"""
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
            ["不应该有资料可用。", '{"is_safe": true}', '{"score": 0.5}', '{"score": 0.5}']
        ),
    )

    cases = [
        EvalCase(
            question="网络连不上怎么办？",
            expected_answer="重启路由器",
            expected_sources=["faq/network.md"],
        )
    ]

    report = await run_eval_suite(
        cases,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tenant_id="t2",
        query_rewrite_enabled=False,
    )

    assert report.average_context_recall == 0.0
