from __future__ import annotations

import logging
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
from app.memory.delayed_confirmation import (
    ensure_delayed_confirmation_schema,
    list_due_confirmations,
    mark_confirmed,
)
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

logger = logging.getLogger(__name__)

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

    错误处理（设计文档 §3a）：单条工单 embedding 失败只跳过该条工单+记
    日志，不影响同批次其它工单——和 scan_and_send_ticket_followups 等
    同批次扫描函数"一条失败不拖垮整批"的隔离原则一致，也和
    consolidation_queue.py::process_pending_jobs 里"每个任务的失败都
    单独捕获，不影响同批次其它任务"是同一种隔离模式。同一张工单可能被
    多个 fix 拿来做相似度比较，embedding 只算一次、缓存在 ticket_id 维度
    的字典里——既避免了 O(fixes × tickets) 的重复调用，也保证一条工单
    embedding 失败只记一次日志、后续 fix 循环直接复用"失败"结果跳过，
    不会对同一条工单反复重试同一次失败。
    """
    await ensure_known_fixes_schema(conn)
    await ensure_ticket_fix_notifications_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    ticket_embeddings: dict[str, list[float] | None] = {}

    async def _embedding_for_ticket(ticket: dict) -> list[float] | None:
        ticket_id = ticket["ticket_id"]
        if ticket_id in ticket_embeddings:
            return ticket_embeddings[ticket_id]
        try:
            embed_result = await embedding_registry.run(
                EmbeddingRequest(texts=[ticket["question"]]),
                provider_name=embedding_provider_name,
            )
            vector: list[float] | None = embed_result.vectors[0]
        except Exception:
            logger.warning(
                "已知修复主动告知扫描：工单 %s 的 embedding 计算失败，跳过该条工单，"
                "不影响同批次其它工单",
                ticket_id,
                exc_info=True,
            )
            vector = None
        ticket_embeddings[ticket_id] = vector
        return vector

    sent_count = 0
    for fix in await list_known_fixes(conn, tenant_id=tenant_id):
        fixed_at = datetime.fromtimestamp(fix["fixed_at"])
        candidates = await list_pending_tickets_created_before(
            conn, tenant_id=tenant_id, before=fixed_at
        )
        for ticket in candidates:
            if await is_already_notified(conn, ticket_id=ticket["ticket_id"], fix_id=fix["fix_id"]):
                continue

            vector = await _embedding_for_ticket(ticket)
            if vector is None:
                continue
            similarity = _cosine_similarity(vector, fix["embedding"])
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


async def scan_and_send_delayed_confirmation_followups(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    channel: ProactiveDeliveryChannel,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    now: datetime,
) -> int:
    """客户表达过"稍后再试"之类的延迟意图后，到期主动确认结果。

    到期项来自 Task 10 的 delayed_confirmations 表（list_due_confirmations
    只返回 confirm_after<=now 且还未确认的记录，天然按租户过滤），发送成功
    才调用 mark_confirmed 标记，被频率限流跳过的项保留待下次扫描重新评估
    ——和 scan_and_send_known_fix_followups/scan_and_send_ticket_followups
    是同一个"扫描+画像/频率治理+发送+记录"模式。
    """
    await ensure_delayed_confirmation_schema(conn)
    await ensure_customer_profile_schema(conn)
    await ensure_followup_log_schema(conn)

    sent_count = 0
    for item in await list_due_confirmations(conn, tenant_id=tenant_id, now=now):
        customer_id = item["user_id"]
        profile = await get_customer_profile(conn, tenant_id=tenant_id, customer_id=customer_id)
        policy = compute_delivery_policy(profile)
        send_history = await get_send_history(
            conn, tenant_id=tenant_id, customer_id=customer_id,
            since=now - timedelta(seconds=policy.window_seconds),
        )
        trigger = FollowupTrigger(
            reason="delayed_confirmation",
            context=f"之前您提到{item['context']}，想确认一下现在情况如何？",
        )
        result = await send_followup_if_allowed(
            trigger, tenant_id=tenant_id, customer_id=customer_id, profile=profile,
            send_history=send_history, now=now, channel=channel,
            llm_registry=llm_registry, llm_provider_name=llm_provider_name,
        )
        if result.sent:
            await record_followup_sent(conn, tenant_id=tenant_id, customer_id=customer_id, sent_at=now)
            await mark_confirmed(conn, confirmation_id=item["id"], now=now)
            sent_count += 1
    return sent_count
