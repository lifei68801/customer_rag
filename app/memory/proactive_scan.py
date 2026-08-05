from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import (
    ensure_ticket_schema,
    list_stale_pending_tickets,
    mark_ticket_notified,
)
from app.memory.customer_profile import ensure_customer_profile_schema, get_customer_profile
from app.memory.delivery_policy import compute_delivery_policy
from app.memory.followup_engine import FollowupTrigger, send_followup_if_allowed
from app.memory.followup_log import ensure_followup_log_schema, get_send_history, record_followup_sent
from app.memory.proactive_channel import ProactiveDeliveryChannel
from app.providers.registry import ProviderRegistry

_DEFAULT_STALE_AFTER_SECONDS = 3 * 24 * 3600


async def scan_and_send_ticket_followups(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    channel: ProactiveDeliveryChannel,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    now: datetime,
    stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
) -> int:
    """主动跟进引擎目前唯一有真实数据支撑的编排入口："工单挂起过久"。

    从 create_ticket() 持久化的工单表里找出还没人工处理、也没跟进过的
    工单（list_stale_pending_tickets），逐个套用客户画像+频率治理决定
    是否/如何发送（send_followup_if_allowed，见 followup_engine.py），
    发送成功才标记工单已跟进+记录发送历史——被频率限流跳过的工单保留
    在待跟进列表里，等下次扫描（部署方用 cron/systemd timer 周期调用，
    不内置常驻循环，和 consolidation_worker.py/incremental_main.py 同一
    个模式）重新评估。

    范围说明：只覆盖"工单挂起过久"这一种触发信号；"已知修复可用"之类
    的触发需要工单-知识库关联这类本仓库没有的数据，不在这次范围内。
    """
    await ensure_ticket_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    stale_tickets = await list_stale_pending_tickets(
        conn, tenant_id=tenant_id, older_than_seconds=stale_after_seconds, now=now
    )
    sent_count = 0
    for ticket in stale_tickets:
        customer_id = ticket["customer_id"]
        profile = await get_customer_profile(
            conn, tenant_id=tenant_id, customer_id=customer_id
        )
        policy = compute_delivery_policy(profile)
        send_history = await get_send_history(
            conn,
            tenant_id=tenant_id,
            customer_id=customer_id,
            since=now - timedelta(seconds=policy.window_seconds),
        )
        stale_hours = stale_after_seconds // 3600
        trigger = FollowupTrigger(
            reason="ticket_pending_too_long",
            context=f"工单「{ticket['question']}」已提交超过{stale_hours}小时仍未处理",
        )
        result = await send_followup_if_allowed(
            trigger,
            tenant_id=tenant_id,
            customer_id=customer_id,
            profile=profile,
            send_history=send_history,
            now=now,
            channel=channel,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        if result.sent:
            await record_followup_sent(
                conn, tenant_id=tenant_id, customer_id=customer_id, sent_at=now
            )
            await mark_ticket_notified(conn, ticket["ticket_id"], now=now)
            sent_count += 1
    return sent_count
