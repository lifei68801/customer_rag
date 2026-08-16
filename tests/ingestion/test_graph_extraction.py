import asyncio
from datetime import datetime

import aiosqlite

from app.graphrag.ontology import Term
from app.graphrag.review_queue import ensure_review_schema, list_pending_reviews
from app.ingestion.graph_extraction import (
    _batch_chunks_by_char_budget,
    extract_and_write_graph_relations,
)
from app.ingestion.chunking import Chunk
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry

_NOW = datetime(2026, 8, 12, 12, 0, 0)

_TERMS = [
    Term(
        tenant_id="t1",
        node_key="示例错误码E502",
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    ),
    Term(
        tenant_id="t1",
        node_key="示例登录模块",
        standard_name="示例登录模块",
        aliases=["示例认证模块"],
        term_type="module",
        product_line="示例产品线",
    ),
]


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []
        self.deleted_sources: list[tuple[str, str]] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
                "provenance": provenance,
                "recorded_at": recorded_at,
            }
        )

    async def delete_relations_by_source(self, source: str, *, tenant_id: str) -> None:
        self.deleted_sources.append((source, tenant_id))
        self.written = [
            item
            for item in self.written
            if not (item["source"] == source and item["tenant_id"] == tenant_id)
        ]


async def test_extracts_normalizes_and_writes_relations_from_chunks():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
            "provenance": "auto_merged",
            "recorded_at": _NOW,
        }
    ]
    assert graph_client.deleted_sources == [("a.md", "t1")]


async def test_unresolved_candidate_goes_to_review_queue_when_review_conn_provided():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "不存在的实体", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    review_conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(review_conn)
    chunks = [Chunk(text="网关超时示例通常与不存在的实体相关", heading_path=[], source="a.md")]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        review_conn=review_conn,
    )

    assert written == 0
    assert graph_client.written == []
    pending = await list_pending_reviews(review_conn, tenant_id="t1")
    assert len(pending) == 1
    assert pending[0]["object_candidate"] == "不存在的实体"


async def test_reingesting_same_source_clears_stale_relations_no_longer_present():
    # 文档内容变更后重新摄取：旧版本抽取出的关系不应该在图谱里永久残留
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
    )
    assert len(graph_client.written) == 1

    # 文档改版后不再提到这组关系，重新摄取应该把旧边清掉，而不是新旧并存
    llm_registry_v2 = ProviderRegistry()
    llm_registry_v2.register(
        ProviderCapability.LLM, "llm", FixedLLMProvider('{"relations": []}')
    )
    new_chunks = [Chunk(text="改版后的无关内容", heading_path=[], source="a.md")]

    await extract_and_write_graph_relations(
        new_chunks,
        llm_registry=llm_registry_v2,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
    )

    assert graph_client.written == []
    assert graph_client.deleted_sources == [("a.md", "t1"), ("a.md", "t1")]


async def test_reingesting_same_source_different_tenant_does_not_delete_other_tenants_edges():
    """跨租户隔离的正面验证：两个租户各自摄取相同相对路径的文档，
    租户 t2 重新摄取不应该删掉租户 t1 已经写入的边——这是本次改动
    顺带修复的 bug（此前 delete_relations_by_source 完全不看租户）。
    """
    llm_registry_t1 = ProviderRegistry()
    llm_registry_t1.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry_t1,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
    )
    assert len(graph_client.written) == 1

    llm_registry_t2 = ProviderRegistry()
    llm_registry_t2.register(
        ProviderCapability.LLM, "llm", FixedLLMProvider('{"relations": []}')
    )
    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry_t2,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t2",
        now=_NOW,
    )

    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
            "source": "a.md",
            "tenant_id": "t1",
            "provenance": "auto_merged",
            "recorded_at": _NOW,
        }
    ]


def test_batch_chunks_by_char_budget_groups_up_to_the_limit():
    chunks = [
        Chunk(text="a" * 1000, heading_path=[], source="a.md"),
        Chunk(text="b" * 1000, heading_path=[], source="a.md"),
        Chunk(text="c" * 1000, heading_path=[], source="a.md"),
    ]

    batches = _batch_chunks_by_char_budget(chunks, max_chars=2500)

    assert len(batches) == 2
    assert len(batches[0]) == 2  # a+b 累计 2000 字符 <= 2500
    assert len(batches[1]) == 1  # c 单独成批


def test_batch_chunks_by_char_budget_keeps_oversized_single_chunk_alone():
    chunks = [
        Chunk(text="x" * 5000, heading_path=[], source="a.md"),
        Chunk(text="y" * 100, heading_path=[], source="a.md"),
    ]

    batches = _batch_chunks_by_char_budget(chunks, max_chars=3000)

    assert len(batches) == 2
    assert len(batches[0]) == 1
    assert batches[0][0].text == "x" * 5000
    assert len(batches[1]) == 1


async def test_extract_and_write_graph_relations_respects_max_concurrency():
    """并发批次数不能超过 max_concurrency——用一个记录"同时在途请求数"的
    fake provider，真的 sleep 一下让并发窗口重叠，验证峰值不超过限制、
    同时确认真的发生了并发（不是退化成串行）。
    """
    concurrent_count = {"current": 0, "peak": 0}

    class TrackingLLMProvider:
        async def complete(self, request):
            concurrent_count["current"] += 1
            concurrent_count["peak"] = max(
                concurrent_count["peak"], concurrent_count["current"]
            )
            await asyncio.sleep(0.05)
            concurrent_count["current"] -= 1
            return ProviderResult(text='{"relations": []}')

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "llm", TrackingLLMProvider())
    graph_client = FakeGraphClient()
    # 20 个 chunk，每个都单独超过 max_chars，逼出 20 个独立批次
    chunks = [Chunk(text="x" * 4000, heading_path=[], source="a.md") for _ in range(20)]

    await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        batch_max_chars=3000,
        max_concurrency=4,
    )

    assert concurrent_count["peak"] <= 4
    assert concurrent_count["peak"] > 1


async def test_one_failing_batch_does_not_prevent_other_batches_from_writing():
    class ContentBasedFailingLLMProvider:
        async def complete(self, request):
            user_content = request.messages[1]["content"]
            if "FAIL_MARKER" in user_content:
                raise RuntimeError("模拟这一批调用失败")
            return ProviderResult(
                text='{"relations": [{"subject": "网关超时示例", '
                '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
            )

    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "llm", ContentBasedFailingLLMProvider())
    graph_client = FakeGraphClient()
    chunks = [
        Chunk(text="FAIL_MARKER" + "a" * 4000, heading_path=[], source="a.md"),
        Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md"),
    ]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
        source="a.md",
        tenant_id="t1",
        now=_NOW,
        batch_max_chars=100,
    )

    assert written == 1
