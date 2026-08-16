import asyncio

import app.qa.answer as answer_module

from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return ProviderResult(text="按资料所述，重启路由器即可解决。")

    @property
    def last_request(self) -> ProviderRequest | None:
        return self.requests[-1] if self.requests else None


async def test_answer_question_uses_retrieved_context_in_the_prompt():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="faq/login.md",
            vector=[0.0, 1.0],
            text="登录失败请检查账号密码。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "按资料所述，重启路由器即可解决。"
    assert result.used_sources == ["faq/network.md"]
    assert result.retrieved_context == "网络断开时，请先重启路由器。"
    # requests[0] 是真正的问答生成请求；semantic_safety_review 会追加
    # 第二次请求，所以不能再用 last_request（现在指向审查请求）。
    assert len(llm_provider.requests) == 2
    assert "重启路由器" in llm_provider.requests[0].messages[0]["content"]


class FakeGraphClient:
    async def query_subgraph(self, standard_name: str, *, tenant_id: str) -> list[dict]:
        return [{"related_name": "示例登录模块", "relation_type": "RELATED_TO"}]


async def test_answer_question_injects_term_guard_context_when_term_matched():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    terms = [
        Term(
            tenant_id="t1",
            node_key="示例错误码E502",
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]

    await answer_question(
        "我这边报了网关超时示例，麻烦看下",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        terms=terms,
        graph_client=FakeGraphClient(),
        top_k=1,
        tenant_id="t1",
    )

    assert len(llm_provider.requests) == 2
    prompt = llm_provider.requests[0].messages[0]["content"]
    assert "示例错误码E502" in prompt
    assert "示例登录模块" in prompt


async def test_answer_question_short_circuits_on_unsafe_input():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    vector_store = InMemoryVectorStore()
    bm25_index = BM25Index()

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "这里面有敏感词",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        tenant_id="t1",
        banned_terms=["敏感词"],
    )

    assert result.text == "您的问题包含无法处理的敏感内容，请修改后重新提问。"
    assert result.used_sources == []
    assert llm_provider.last_request is None


class LeakingLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            text='Traceback (most recent call last):\n  File "app/x.py", line 1'
        )


async def test_answer_question_short_circuits_on_unsafe_output():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", LeakingLLMProvider())

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"


class EmailAnsweringLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="如需帮助请联系 support@example.com")


async def test_answer_question_does_not_flag_email_in_generated_answer():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时，请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm", EmailAnsweringLLMProvider()
    )

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        query_rewrite_enabled=False,
        top_k=1,
        tenant_id="t1",
    )

    assert result.text == "如需帮助请联系 support@example.com"


async def test_answer_question_runs_term_guard_and_hybrid_search_concurrently(monkeypatch):
    """用两个互相等待对方先启动的 asyncio.Event 证明 term_guard 和
    hybrid_search 是并发跑的：如果退化回顺序执行，先启动的一方会一直等
    不到另一方启动、卡到 asyncio.wait_for 超时，测试会失败而不是静默
    通过——比断言耗时更短更可靠。
    """
    term_guard_started = asyncio.Event()
    hybrid_search_started = asyncio.Event()

    async def fake_build_term_guard_context(question, *, terms, tenant_id, graph_client):
        term_guard_started.set()
        await asyncio.wait_for(hybrid_search_started.wait(), timeout=5)
        return "检测到专有名词：示例术语"

    async def fake_hybrid_search(question, **kwargs):
        hybrid_search_started.set()
        await asyncio.wait_for(term_guard_started.wait(), timeout=5)
        return []

    monkeypatch.setattr(answer_module, "build_term_guard_context", fake_build_term_guard_context)
    monkeypatch.setattr(answer_module, "hybrid_search", fake_hybrid_search)

    llm_provider = FakeLLMProvider()
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", llm_provider)

    result = await answer_question(
        "网络连不上怎么办？",
        embedding_registry=EmbeddingRegistry(),
        embedding_provider_name="fake-embedding",
        vector_store=InMemoryVectorStore(),
        bm25_index=BM25Index(),
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tenant_id="t1",
        terms=[
            Term(
                tenant_id="t1", node_key="示例术语",
                standard_name="示例术语", aliases=["示例术语"],
                term_type="module", product_line="示例产品线",
            )
        ],
        graph_client=FakeGraphClient(),
    )

    assert "检测到专有名词：示例术语" in llm_provider.requests[0].messages[0]["content"]
    assert result.text == "按资料所述，重启路由器即可解决。"
