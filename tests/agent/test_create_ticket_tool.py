from app.agent.create_ticket_tool import create_ticket


async def test_create_ticket_returns_a_ticket_id():
    result = await create_ticket(
        question="登录不了怎么办",
        reason="检索结果不足，需人工介入",
    )

    assert result.ticket_id
    assert result.reason == "检索结果不足，需人工介入"
