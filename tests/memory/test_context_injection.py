import aiosqlite

from app.memory.context_injection import inject_memory_context
from app.memory.memory_store import upsert_memory_item
from app.memory.schema import ensure_schema
from app.memory.session_window import append_turn
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


async def test_injects_recent_turns_and_active_memory_items():
    conn = await _connect()
    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user", content="你好"
    )
    await append_turn(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        role="assistant",
        content="您好",
    )
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1", text="客户使用企业版套餐"
    )

    messages = await inject_memory_context(
        conn, tenant_id="t1", session_id="s1", user_id="u1", recent_turn_limit=10
    )

    assert any("客户使用企业版套餐" in m["content"] for m in messages if m["role"] == "system")
    assert {"role": "user", "content": "你好"} in [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]


async def test_compacts_turns_beyond_preserve_limit():
    conn = await _connect()
    for i in range(10):
        await append_turn(
            conn,
            tenant_id="t1",
            session_id="s1",
            user_id="u1",
            role="user",
            content=f"msg{i}",
        )

    messages = await inject_memory_context(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        recent_turn_limit=10,
        compaction_preserve_recent_messages=4,
    )

    turn_messages = [m for m in messages if m["role"] == "user"]
    assert len(turn_messages) == 4
    assert any("会话摘要" in m["content"] for m in messages if m["role"] == "system")


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


async def test_uses_fused_recall_when_question_and_embedding_registry_provided():
    conn = await _connect()
    await upsert_memory_item(
        conn, memory_id="m1", tenant_id="t1", user_id="u1",
        text="客户使用企业版套餐", embedding=[1.0, 0.0],
    )
    await upsert_memory_item(
        conn, memory_id="m2", tenant_id="t1", user_id="u1",
        text="客户提到过路由器型号", embedding=[1.0, 0.0],
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    messages = await inject_memory_context(
        conn,
        tenant_id="t1",
        session_id="s1",
        user_id="u1",
        question="网络连不上怎么办？",
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        memory_item_limit=1,
    )

    system_messages = [m for m in messages if m["role"] == "system"]
    joined = "\n".join(m["content"] for m in system_messages)
    # memory_item_limit=1 时融合召回只应该注入一条,不是简单按更新时间截断
    assert joined.count("客户") == 1


async def test_safety_fallback_reply_in_history_is_not_fed_back_verbatim():
    """历史里的"未通过安全审查"不能原样喂给 LLM。

    demo 租户一次回答被语义审查误拦后，同一会话里再问同一个问题，模型一个
    工具都没调、直接复述了那句兜底文案——它把那句话当成了自己上一轮的回答。
    """
    from app.memory.context_injection import SAFETY_FALLBACK_HISTORY_NOTE
    from app.safety.rules import UNSAFE_INPUT_MESSAGE, UNSAFE_OUTPUT_MESSAGE

    conn = await _connect()
    for role, content in [
        ("user", "Coca-Cola 下有多少订单"),
        ("assistant", "Coca-Cola 下共有 10 个订单。"),
        ("user", "订单号分别是什么"),
        ("assistant", UNSAFE_OUTPUT_MESSAGE),
        ("user", "某个不安全的输入"),
        ("assistant", UNSAFE_INPUT_MESSAGE),
    ]:
        await append_turn(
            conn, tenant_id="t1", session_id="s1", user_id="u1", role=role, content=content
        )

    messages = await inject_memory_context(
        conn, tenant_id="t1", session_id="s1", user_id="u1", recent_turn_limit=10
    )
    turns = [(m["role"], m["content"]) for m in messages if m["role"] != "system"]

    contents = [c for _, c in turns]
    assert UNSAFE_OUTPUT_MESSAGE not in contents
    assert UNSAFE_INPUT_MESSAGE not in contents
    # 替换而不是删掉：一问一答的交替不能断，用户那一问也要留着——后面的追问
    # 靠它才知道"订单号"指的是哪个产品的订单。
    assert [r for r, _ in turns] == ["user", "assistant"] * 3
    assert ("user", "订单号分别是什么") in turns
    assert contents.count(SAFETY_FALLBACK_HISTORY_NOTE) == 2
    # 正常回答原样保留。
    assert ("assistant", "Coca-Cola 下共有 10 个订单。") in turns


async def test_user_turn_quoting_the_fallback_text_is_left_alone():
    """只替换助手说的兜底文案。用户自己把这句话贴进来问"为什么"，那是他的问题原文。"""
    from app.safety.rules import UNSAFE_OUTPUT_MESSAGE

    conn = await _connect()
    await append_turn(
        conn, tenant_id="t1", session_id="s1", user_id="u1", role="user", content=UNSAFE_OUTPUT_MESSAGE
    )

    messages = await inject_memory_context(
        conn, tenant_id="t1", session_id="s1", user_id="u1", recent_turn_limit=10
    )

    assert {"role": "user", "content": UNSAFE_OUTPUT_MESSAGE} in [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
