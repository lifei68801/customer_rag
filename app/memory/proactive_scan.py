from __future__ import annotations

import math
from datetime import datetime, timedelta

import aiosqlite

from app.agent.create_ticket_tool import (
    ensure_ticket_schema,
    list_pending_tickets_created_before,
    list_stale_pending_tickets,
    mark_ticket_notified,
)
from app.memory.customer_profile import ensure_customer_profile_schema, get_customer_profile
from app.memory.delivery_policy import compute_delivery_policy
from app.memory.followup_engine import FollowupTrigger, send_followup_if_allowed
from app.memory.followup_log import ensure_followup_log_schema, get_send_history, record_followup_sent
from app.memory.known_fixes import ensure_known_fixes_schema, list_known_fixes
from app.memory.proactive_channel import ProactiveDeliveryChannel
from app.memory.ticket_fix_notifications import (
    ensure_ticket_fix_notifications_schema,
    is_already_notified,
    mark_notified,
)
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest
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


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def scan_and_send_known_fix_followups(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    channel: ProactiveDeliveryChannel,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    now: datetime,
    similarity_threshold: float = 0.5,
) -> int:
    """已知故障修复后主动告知：对每条登记过的 known_fix，找出修复之前
    提交、且还没为这条 fix 通知过的 pending 工单，语义相似度够高就主动
    告知客户问题已修复。

    不复用 tickets.notified_at（那是"挂起过久"触发专用标记）——同一张
    工单可能先被挂起过久通知过、之后又该被已修复通知，两者不能共用同一
    个布尔标记互相掩盖，去重靠独立的 ticket_fix_notifications 表按
    (ticket_id, fix_id) 维度判断。
    """
    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    sent_count = 0
    for fix in await list_known_fixes(conn, tenant_id=tenant_id):
        fixed_at = datetime.fromtimestamp(fix["fixed_at"])
        candidates = await list_pending_tickets_created_before(
            conn, tenant_id=tenant_id, before=fixed_at
        )
        for ticket in candidates:
            if await is_already_notified(conn, ticket_id=ticket["ticket_id"], fix_id=fix["fix_id"]):
                continue

            embed_result = await embedding_registry.run(
                EmbeddingRequest(texts=[ticket["question"]]),
                provider_name=embedding_provider_name,
            )
            similarity = _cosine_similarity(embed_result.vectors[0], fix["embedding"])
            if similarity < similarity_threshold:
                continue

            customer_id = ticket["customer_id"]
            profile = await get_customer_profile(conn, tenant_id=tenant_id, customer_id=customer_id)
            policy = compute_delivery_policy(profile)
            send_history = await get_send_history(
                conn, tenant_id=tenant_id, customer_id=customer_id,
                since=now - timedelta(seconds=policy.window_seconds),
            )
            trigger = FollowupTrigger(
                reason="known_fix_available",
                context=f"您反馈的「{ticket['question']}」问题已修复",
            )
            result = await send_followup_if_allowed(
                trigger, tenant_id=tenant_id, customer_id=customer_id, profile=profile,
                send_history=send_history, now=now, channel=channel,
                llm_registry=llm_registry, llm_provider_name=llm_provider_name,
            )
            if result.sent:
                await record_followup_sent(conn, tenant_id=tenant_id, customer_id=customer_id, sent_at=now)
                await mark_notified(conn, ticket_id=ticket["ticket_id"], fix_id=fix["fix_id"], now=now)
                sent_count += 1
    return sent_count
