from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

import aiosqlite

from app.config.settings import Settings
from app.memory.factory import build_memory_conn_from_settings
from app.memory.proactive_channel import MockProactiveChannel, ProactiveDeliveryChannel
from app.memory.proactive_scan import (
    scan_and_send_delayed_confirmation_followups,
    scan_and_send_known_fix_followups,
    scan_and_send_ticket_followups,
)
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry


async def main(
    *,
    tenant_id: str,
    settings: Settings | None = None,
    memory_conn: aiosqlite.Connection | None = None,
    llm_registry: ProviderRegistry | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    channel: ProactiveDeliveryChannel | None = None,
    now: datetime | None = None,
    stale_after_hours: int = 72,
) -> int:
    """主动跟进扫描的处理入口：跑一遍全部三种主动跟进扫描并发送——"工单
    挂起过久"（Task 1）、"已知修复可用主动告知"（Task 8）、"延迟意图到期
    确认"（Task 13）。后两种扫描函数在各自任务里已经实现并测试完备，但
    在这个任务把它们真正接入生产入口之前，一直是"建了但没人调用"的
    死代码——consolidation worker 里 Task 12 新增的 detect_delay_intent
    LLM 调用每次都会无条件执行、往 delayed_confirmations 表写记录，如果
    没有人读取/处理这些记录，就是纯粹的持续成本、零交付价值。

    和 consolidation_worker.py/incremental_main.py 同一个部署模式：不内置
    常驻循环，只提供"跑一次就退出"的单次入口，交给 cron/systemd timer
    周期调用；一次调用只扫描一个租户，三种扫描依次跑完再返回，不额外引入
    调度/循环基础设施。

    channel 默认是 MockProactiveChannel——本仓库没有接入任何真实的短信/
    邮件/App push 触达渠道，真实对接需要业务方提供具体渠道后另行实现
    ProactiveDeliveryChannel（见 proactive_channel.py），部署时通过这个
    参数注入。

    返回值是三种扫描各自发送条数之和；终端打印同时给出三者的分项和合计，
    方便从 cron 日志里直接看出是哪种触发在起作用。

    用法：python -m app.memory.proactive_scan_worker --tenant-id t1
    """
    resolved_settings = settings or Settings()
    conn = memory_conn or await build_memory_conn_from_settings(resolved_settings)
    llm = llm_registry or build_llm_registry_from_settings(resolved_settings)
    embedding = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    resolved_channel = channel or MockProactiveChannel()
    resolved_now = now or datetime.now()

    ticket_sent = await scan_and_send_ticket_followups(
        conn,
        tenant_id=tenant_id,
        channel=resolved_channel,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        now=resolved_now,
        stale_after_seconds=stale_after_hours * 3600,
    )
    known_fix_sent = await scan_and_send_known_fix_followups(
        conn,
        tenant_id=tenant_id,
        channel=resolved_channel,
        embedding_registry=embedding,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        now=resolved_now,
    )
    delayed_confirmation_sent = await scan_and_send_delayed_confirmation_followups(
        conn,
        tenant_id=tenant_id,
        channel=resolved_channel,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        now=resolved_now,
    )

    total_sent = ticket_sent + known_fix_sent + delayed_confirmation_sent
    print(
        f"本次为租户 {tenant_id} 发出 {total_sent} 条主动跟进消息"
        f"（工单挂起过久 {ticket_sent} 条，已知修复告知 {known_fix_sent} 条，"
        f"延迟确认 {delayed_confirmation_sent} 条）"
    )
    return total_sent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描挂起过久的工单并发送主动跟进")
    parser.add_argument("--tenant-id", required=True, help="本次扫描的租户标识")
    parser.add_argument(
        "--stale-after-hours", type=int, default=72, help="工单挂起超过多少小时算需要跟进"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(tenant_id=args.tenant_id, stale_after_hours=args.stale_after_hours)
    )
