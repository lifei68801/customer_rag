from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import create_ticket
from app.memory.delayed_confirmation import (
    ensure_delayed_confirmation_schema,
    schedule_delayed_confirmation,
)
from app.memory.known_fixes import ensure_known_fixes_schema, register_known_fix
from app.memory.proactive_channel import MockProactiveChannel
from app.memory.proactive_scan_worker import main
from app.memory.ticket_fix_notifications import ensure_ticket_fix_notifications_schema
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.factory import DEFAULT_EMBEDDING_PROVIDER_NAME, DEFAULT_LLM_PROVIDER_NAME
from app.providers.registry import ProviderRegistry
from tests.settings_factory import build_settings

_STALE_AFTER_HOURS = 72


def _settings():
    return build_settings()


class ScriptedLLMProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


class FixedEmbeddingProvider:
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[self._vector for _ in request.texts])


def _llm_registry(responses: list[str]) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(ProviderCapability.LLM, DEFAULT_LLM_PROVIDER_NAME, ScriptedLLMProvider(responses))
    return registry


def _embedding_registry(vector: list[float]) -> EmbeddingRegistry:
    registry = EmbeddingRegistry()
    registry.register(DEFAULT_EMBEDDING_PROVIDER_NAME, FixedEmbeddingProvider(vector))
    return registry


async def test_main_runs_all_three_scanners_and_sums_sent_counts(capsys):
    """回归测试 Finding 4：Task 8/13 分别实现的
    scan_and_send_known_fix_followups / scan_and_send_delayed_confirmation_followups
    在这个任务之前从未被生产入口 main() 调用过——main() 原来只跑
    scan_and_send_ticket_followups 一种扫描。这里为三种触发信号各准备
    一条命中数据，断言 main() 三种扫描都真正跑了、返回值是三者之和、
    终端打印里三个分项数字都能看到。
    """
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)

    # 触发信号一：工单挂起过久（Task 1）
    await create_ticket(
        tenant_id="t1", customer_id="c1", question="登录不了怎么办",
        reason="原因", conn=conn, now=now - timedelta(hours=100),
    )

    # 触发信号二：已知修复可用主动告知（Task 8）
    fixed_at = now - timedelta(days=1)
    fix_embedding_registry = _embedding_registry([1.0, 0.0])
    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    await register_known_fix(
        conn, tenant_id="t1", description="网关超时问题已修复", fixed_at=fixed_at,
        embedding_registry=fix_embedding_registry, embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
    )
    await create_ticket(
        tenant_id="t1", customer_id="c2", question="网关超时报错E502",
        reason="原因", conn=conn, now=fixed_at - timedelta(days=1),
    )

    # 触发信号三：延迟意图到期确认（Task 13）
    await ensure_delayed_confirmation_schema(conn)
    await schedule_delayed_confirmation(
        conn, tenant_id="t1", user_id="c3", context="之前反馈的登录问题",
        confirm_after=now - timedelta(minutes=1),
    )

    channel = MockProactiveChannel()
    # 三种扫描各自触发一次发送，llm_registry 依次提供三条脚本响应
    llm_registry = _llm_registry(
        [
            "工单挂起过久的跟进消息",
            "已知修复的跟进消息",
            "延迟确认的跟进消息",
        ]
    )
    embedding_registry = _embedding_registry([1.0, 0.0])

    total = await main(
        tenant_id="t1",
        settings=_settings(),
        memory_conn=conn,
        llm_registry=llm_registry,
        embedding_registry=embedding_registry,
        channel=channel,
        now=now,
        stale_after_hours=_STALE_AFTER_HOURS,
    )

    assert total == 3
    assert len(channel.sent) == 3
    assert {item["customer_id"] for item in channel.sent} == {"c1", "c2", "c3"}

    printed = capsys.readouterr().out
    assert "3" in printed
    assert "工单挂起过久 1" in printed
    assert "已知修复告知 1" in printed
    assert "延迟确认 1" in printed


async def test_main_ignores_other_tenants_across_all_three_scanners():
    conn = await aiosqlite.connect(":memory:")
    now = datetime(2026, 8, 6, 10, 0, 0)

    await create_ticket(
        tenant_id="other-tenant", customer_id="c1", question="别的租户的问题",
        reason="原因", conn=conn, now=now - timedelta(hours=100),
    )
    await ensure_delayed_confirmation_schema(conn)
    await schedule_delayed_confirmation(
        conn, tenant_id="other-tenant", user_id="c2", context="context",
        confirm_after=now - timedelta(minutes=1),
    )

    channel = MockProactiveChannel()
    llm_registry = _llm_registry([])
    embedding_registry = _embedding_registry([1.0, 0.0])

    total = await main(
        tenant_id="t1",
        settings=_settings(),
        memory_conn=conn,
        llm_registry=llm_registry,
        embedding_registry=embedding_registry,
        channel=channel,
        now=now,
        stale_after_hours=_STALE_AFTER_HOURS,
    )

    assert total == 0
    assert channel.sent == []
