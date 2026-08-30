from __future__ import annotations

from typing import Protocol


class ProactiveDeliveryChannel(Protocol):
    """主动触达渠道的抽象接口——真实短信/邮件/App push 对接不在本仓库
    范围内（本项目没有接入任何真实的客户触达渠道），和
    app/memory/tickets.py 是同一个"先定抽象接口+mock实现，
    留出真实对接点"的做法。
    """

    async def send(self, *, customer_id: str, message: str) -> None: ...


class MockProactiveChannel:
    """mock 实现：只把要发的消息记下来，不真的发送到任何地方。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, customer_id: str, message: str) -> None:
        self.sent.append({"customer_id": customer_id, "message": message})
