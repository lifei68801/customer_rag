from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import create_ticket, list_stale_pending_tickets
from app.memory.customer_profile import ensure_customer_profile_schema, upsert_customer_profile
from app.memory.followup_log import ensure_followup_log_schema, get_send_history
from app.memory.proactive_channel import MockProactiveChannel
from app.memory.proactive_scan import scan_and_send_ticket_followups
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
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
