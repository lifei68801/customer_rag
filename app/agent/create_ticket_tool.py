from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CreateTicketResult:
    ticket_id: str
    reason: str


async def create_ticket(*, question: str, reason: str) -> CreateTicketResult:
    """转人工工单的 mock 实现，预留真实工单系统对接接口。

    真实对接（工单系统的具体 API）不在本仓库范围内，需与客服工单
    系统负责方另行排期——见执行计划第5节跨职能依赖清单。
    """
    return CreateTicketResult(ticket_id=str(uuid.uuid4()), reason=reason)
