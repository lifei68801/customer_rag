from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import (
    create_ticket,
    list_pending_tickets_created_before,
    list_stale_pending_tickets,
)
from app.memory.customer_profile import ensure_customer_profile_schema, upsert_customer_profile
from app.memory.followup_log import ensure_followup_log_schema, get_send_history
from app.memory.known_fixes import ensure_known_fixes_schema, register_known_fix
from app.memory.proactive_channel import MockProactiveChannel
from app.memory.proactive_scan import scan_and_send_known_fix_followups, scan_and_send_ticket_followups
from app.memory.ticket_fix_notifications import ensure_ticket_fix_notifications_schema, is_already_notified
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry

_STALE_AFTER_SECONDS = 72 * 3600


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


def _llm_registry(responses: list[str]) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, "llm", ScriptedLLMProvider(responses))
    return registry


class FixedEmbeddingProvider:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[self._vector for _ in request.texts])


def _embedding_registry(vector: list[float]) -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register("fake-embedding", FixedEmbeddingProvider(vector))
    return registry


async def test_scans_stale_ticket_and_sends_followup_then_marks_it_notified():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)
    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="检索结果不足，需人工介入",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["您好，关于您的工单我们想跟进一下。"])

    sent = await scan_and_send_ticket_followups(
        conn,
        tenant_id="t1",
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
        stale_after_seconds=_STALE_AFTER_SECONDS,
    )

    assert sent == 1
    assert channel.sent == [
        {"customer_id": "c1", "message": "您好，关于您的工单我们想跟进一下。"}
    ]

    # 已经跟进过的工单不应该在下次扫描里重复出现
    remaining = await list_stale_pending_tickets(
        conn, tenant_id="t1", older_than_seconds=_STALE_AFTER_SECONDS, now=now
    )
    assert remaining == []


async def test_records_the_send_in_followup_log():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)
    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["跟进消息"])

    await scan_and_send_ticket_followups(
        conn,
        tenant_id="t1",
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
        stale_after_seconds=_STALE_AFTER_SECONDS,
    )

    await ensure_followup_log_schema(conn)
    history = await get_send_history(
        conn, tenant_id="t1", customer_id="c1", since=now - timedelta(days=1)
    )
    assert history == [now]


async def test_does_not_resend_when_rate_limited_and_keeps_ticket_pending():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)
    await ensure_followup_log_schema(conn)
    from app.memory.followup_log import record_followup_sent

    # 客户1小时前刚收到过一条跟进消息，默认策略下未到最小发送间隔
    await record_followup_sent(
        conn, tenant_id="t1", customer_id="c1", sent_at=now - timedelta(hours=1)
    )
    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_ticket_followups(
        conn,
        tenant_id="t1",
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
        stale_after_seconds=_STALE_AFTER_SECONDS,
    )

    assert sent == 0
    assert channel.sent == []

    # 被限流跳过的工单应该保留在待跟进列表里，供下次扫描重新评估
    remaining = await list_stale_pending_tickets(
        conn, tenant_id="t1", older_than_seconds=_STALE_AFTER_SECONDS, now=now
    )
    assert len(remaining) == 1


async def test_ignores_stale_tickets_from_other_tenants():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)
    await create_ticket(
        tenant_id="t2",
        customer_id="c1",
        question="另一个租户的问题",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    channel = MockProactiveChannel()
    llm_registry = _llm_registry([])

    sent = await scan_and_send_ticket_followups(
        conn,
        tenant_id="t1",
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
        stale_after_seconds=_STALE_AFTER_SECONDS,
    )

    assert sent == 0
    assert channel.sent == []


async def test_uses_customer_profile_communication_style_when_present():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 5, 10, 0, 0)
    await ensure_customer_profile_schema(conn)
    await upsert_customer_profile(
        conn,
        tenant_id="t1",
        customer_id="c1",
        is_vip=True,
        feedback_label="neutral",
        communication_style="casual",
    )
    await create_ticket(
        tenant_id="t1",
        customer_id="c1",
        question="登录不了怎么办",
        reason="原因",
        conn=conn,
        now=now - timedelta(hours=100),
    )
    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["亲切随和风格的跟进消息"])

    sent = await scan_and_send_ticket_followups(
        conn,
        tenant_id="t1",
        channel=channel,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        now=now,
        stale_after_seconds=_STALE_AFTER_SECONDS,
    )

    assert sent == 1
    assert channel.sent[0]["message"] == "亲切随和风格的跟进消息"


async def test_scan_and_send_known_fix_followups_matches_similar_ticket():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    embedding_registry = _embedding_registry([1.0, 0.0])
    fix_id = await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="网关超时报错E502",
        reason="原因", conn=conn, now=fixed_at - timedelta(days=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["您反馈的网关超时问题已经修复，感谢您的耐心等待。"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 1
    assert len(channel.sent) == 1
    tickets = await list_pending_tickets_created_before(conn, tenant_id="t1", before=now)
    assert await is_already_notified(conn, ticket_id=tickets[0]["ticket_id"], fix_id=fix_id) is True


async def test_scan_and_send_known_fix_followups_skips_dissimilar_ticket():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    fix_embedding_registry = _embedding_registry([1.0, 0.0])
    await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=fix_embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="完全不相关的问题",
        reason="原因", conn=conn, now=fixed_at - timedelta(days=1),
    )

    channel = MockProactiveChannel()
    # 扫描时给一个和 fix embedding 正交的向量，模拟"语义不相关"
    scan_embedding_registry = _embedding_registry([0.0, 1.0])
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=scan_embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 0
    assert channel.sent == []


async def test_scan_and_send_known_fix_followups_excludes_tickets_created_after_fix():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)
    fixed_at = datetime(2026, 8, 5, 0, 0, 0)

    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    embedding_registry = _embedding_registry([1.0, 0.0])
    await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
    )
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="网关超时报错E502（修复之后才提的）",
        reason="原因", conn=conn, now=fixed_at + timedelta(days=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry(["不应该被用到"])

    sent = await scan_and_send_known_fix_followups(
        conn, tenant_id="t1", channel=channel,
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        llm_registry=llm_registry, llm_provider_name="llm", now=now,
    )

    assert sent == 0
