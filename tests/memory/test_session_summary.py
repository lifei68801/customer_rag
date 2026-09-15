import aiosqlite
import pytest

from app.memory.context_injection import inject_memory_context
from app.memory.schema import ensure_schema
from app.memory.session_summary import (
    ensure_session_summary_schema,
    get_session_summary,
    plan_summary_update,
    render_turns,
    save_session_summary,
    summarize_turns,
)
from app.memory.session_window import append_turn, get_turns_with_ids
from app.memory.session_summary_worker import refresh_session_summary
from app.providers.base import ProviderResult

pytestmark = pytest.mark.anyio


class ScriptedLLM:
    """按脚本回答的 LLM；raises=True 时每次调用都抛（模拟 LLM 不可用）。"""

    def __init__(self, replies: list[str] | None = None, *, raises: bool = False) -> None:
        self.replies = list(replies or [])
        self.raises = raises
        self.prompts: list[str] = []

    async def run(self, capability, request, *, provider_name):
        self.prompts.append(request.messages[-1]["content"])
        if self.raises:
            raise RuntimeError("LLM 挂了")
        return ProviderResult(text=self.replies.pop(0))


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    await ensure_session_summary_schema(conn)
    return conn


async def _seed(conn, count: int, *, tenant_id: str = "t1", session_id: str = "s1") -> None:
    for i in range(count):
        await append_turn(
            conn, tenant_id=tenant_id, session_id=session_id, user_id="u1",
            role="user" if i % 2 == 0 else "assistant", content=f"第{i}条",
        )


# ── 该摘哪几条 ──────────────────────────────────────────────────────────


async def test_nothing_to_summarize_while_everything_still_fits_in_the_window():
    conn = await _conn()
    await _seed(conn, 8)
    turns = await get_turns_with_ids(conn, tenant_id="t1", session_id="s1")

    assert plan_summary_update(turns, preserve_recent_messages=8, covered_through_turn_id=0) == []
    await conn.close()


async def test_only_the_turns_pushed_out_of_the_window_get_summarized():
    """判据必须跟问答那一侧一致：最近 N 条原样保留，再往前的才进摘要。
    两边不一致会让同一条要么在上下文里出现两次，要么消失。"""
    conn = await _conn()
    await _seed(conn, 11)
    turns = await get_turns_with_ids(conn, tenant_id="t1", session_id="s1")

    pending = plan_summary_update(turns, preserve_recent_messages=8, covered_through_turn_id=0)

    assert [t["content"] for t in pending] == ["第0条", "第1条", "第2条"]
    await conn.close()


async def test_already_summarized_turns_are_not_summarized_again():
    """增量：只摘上次摘完之后新压缩掉的那几条，成本不随会话长度增长。"""
    conn = await _conn()
    await _seed(conn, 13)
    turns = await get_turns_with_ids(conn, tenant_id="t1", session_id="s1")
    covered = int(turns[2]["id"])

    pending = plan_summary_update(
        turns, preserve_recent_messages=8, covered_through_turn_id=covered
    )

    assert [t["content"] for t in pending] == ["第3条", "第4条"]
    await conn.close()


# ── 摘要本身 ────────────────────────────────────────────────────────────


async def test_summarize_merges_the_previous_summary_instead_of_redoing_everything():
    llm = ScriptedLLM(["用户要 2024 年的数据，已确认订单 3353 笔。"])
    turns = [{"role": "user", "content": "只要 2024 年的"}, {"role": "assistant", "content": "好的"}]

    summary = await summarize_turns(
        previous_summary="用户在问订单数量。", turns=turns,
        llm_registry=llm, llm_provider_name="fake",
    )

    assert summary == "用户要 2024 年的数据，已确认订单 3353 笔。"
    # 旧摘要原样带进提示词：全量重摘的成本随轮数线性增长，措辞也会每次不同。
    assert "用户在问订单数量。" in llm.prompts[0]
    assert "只要 2024 年的" in llm.prompts[0]


async def test_summarize_returns_none_when_the_model_fails():
    llm = ScriptedLLM(raises=True)

    assert await summarize_turns(
        previous_summary=None, turns=[{"role": "user", "content": "x"}],
        llm_registry=llm, llm_provider_name="fake",
    ) is None


def test_render_turns_labels_speakers_in_chinese():
    text = render_turns([{"role": "user", "content": "问"}, {"role": "assistant", "content": "答"}])

    assert text == "用户：问\n助手：答"


# ── worker ──────────────────────────────────────────────────────────────


async def test_worker_writes_the_summary_and_records_how_far_it_got():
    conn = await _conn()
    await _seed(conn, 11)
    llm = ScriptedLLM(["前三条在寒暄。"])

    assert await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1", llm_registry=llm, llm_provider_name="fake",
    ) is True

    stored = await get_session_summary(conn, tenant_id="t1", session_id="s1")
    assert stored is not None
    assert stored.summary == "前三条在寒暄。"
    turns = await get_turns_with_ids(conn, tenant_id="t1", session_id="s1")
    assert stored.covered_through_turn_id == int(turns[2]["id"])
    await conn.close()


async def test_a_failed_summary_does_not_advance_the_cursor():
    """推进了游标就等于宣称"这几条已经摘过了"，而它们其实哪儿都没进去——
    这段历史会永久消失。"""
    conn = await _conn()
    await _seed(conn, 11)

    assert await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1",
        llm_registry=ScriptedLLM(raises=True), llm_provider_name="fake",
    ) is False
    assert await get_session_summary(conn, tenant_id="t1", session_id="s1") is None

    # 下一次重试仍然能摘到那几条。
    assert await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1",
        llm_registry=ScriptedLLM(["补上了。"]), llm_provider_name="fake",
    ) is True
    await conn.close()


async def test_worker_is_a_noop_for_a_short_session():
    conn = await _conn()
    await _seed(conn, 4)
    llm = ScriptedLLM([])

    assert await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1", llm_registry=llm, llm_provider_name="fake",
    ) is False
    assert llm.prompts == []
    await conn.close()


# ── 问答侧怎么用它 ──────────────────────────────────────────────────────


async def test_context_uses_the_llm_summary_and_drops_the_statistical_one():
    conn = await _conn()
    await _seed(conn, 11)
    await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1",
        llm_registry=ScriptedLLM(["用户要 2024 年的数据。"]), llm_provider_name="fake",
    )

    messages = await inject_memory_context(conn, tenant_id="t1", session_id="s1", user_id="u1")

    systems = [m["content"] for m in messages if m["role"] == "system"]
    assert any("用户要 2024 年的数据。" in s for s in systems)
    # 两条摘要说同一段历史，其中一条只说得出条数，放在一起只会稀释另一条。
    assert not any("共压缩" in s for s in systems)
    await conn.close()


async def test_summarized_turns_are_not_repeated_verbatim():
    """已经进摘要的轮次不再原样带进上下文——否则同一句话出现两次。"""
    conn = await _conn()
    await _seed(conn, 11)
    await refresh_session_summary(
        conn, tenant_id="t1", session_id="s1",
        llm_registry=ScriptedLLM(["摘要正文"]), llm_provider_name="fake",
    )

    messages = await inject_memory_context(conn, tenant_id="t1", session_id="s1", user_id="u1")

    contents = [m["content"] for m in messages]
    assert "第0条" not in contents and "第2条" not in contents
    assert "第3条" in contents and "第10条" in contents
    await conn.close()


async def test_without_a_summary_the_behaviour_is_exactly_what_it_was_before():
    """worker 没跑过、或刚失败：回落统计摘要。降级后的行为跟接入前逐字相同。"""
    conn = await _conn()
    await _seed(conn, 11)

    messages = await inject_memory_context(conn, tenant_id="t1", session_id="s1", user_id="u1")

    systems = [m["content"] for m in messages if m["role"] == "system"]
    assert any("共压缩" in s for s in systems)
    await conn.close()


async def test_a_missing_summary_table_does_not_break_the_answer_path():
    """老库还没建这张表时，问答不能因此报错。"""
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)  # 故意不建 session_summaries
    await _seed(conn, 11)

    messages = await inject_memory_context(conn, tenant_id="t1", session_id="s1", user_id="u1")

    assert any("共压缩" in m["content"] for m in messages if m["role"] == "system")
    await conn.close()


async def test_summaries_are_scoped_to_one_session():
    conn = await _conn()
    await _seed(conn, 11, session_id="s1")
    await _seed(conn, 11, session_id="s2")
    await save_session_summary(
        conn, tenant_id="t1", session_id="s1", summary="只属于 s1", covered_through_turn_id=3,
    )

    messages = await inject_memory_context(conn, tenant_id="t1", session_id="s2", user_id="u1")

    assert not any("只属于 s1" in m["content"] for m in messages)
    await conn.close()


async def test_worker_does_not_starve_old_sessions_behind_a_crowd_of_short_ones():
    """limit 限制的是"更新了几个"，不是"看了几个"。

    真实库里 140 个会话有 130 个是 2 轮的测试残留，它们比需要摘要的那几个
    更晚活跃。按"最近 N 个"截断的话，要做的工作永远排不进来——第一次跑这个
    worker 就是这样：--limit 20 取最近 20 个全是 2 轮的，结果"更新 0 个"。
    """
    from app.memory.session_summary_worker import main

    conn = await _conn()
    await _seed(conn, 11, session_id="需要摘要的老会话")
    for i in range(30):  # 之后活跃的一堆短会话
        await _seed(conn, 2, session_id=f"短会话{i}")

    updated = await main(
        memory_conn=conn, llm_registry=ScriptedLLM(["摘要正文"]), limit=5,
    )

    assert updated == 1
    assert await get_session_summary(conn, tenant_id="t1", session_id="需要摘要的老会话") is not None
    await conn.close()


async def test_worker_stops_after_updating_limit_sessions():
    from app.memory.session_summary_worker import main

    conn = await _conn()
    for i in range(4):
        await _seed(conn, 11, session_id=f"长会话{i}")

    updated = await main(
        memory_conn=conn, llm_registry=ScriptedLLM(["摘要"] * 4), limit=2,
    )

    assert updated == 2
    await conn.close()


async def test_candidate_list_excludes_sessions_that_cannot_need_a_summary():
    """轮数不足滑窗的会话在 SQL 里就滤掉，不进候选列表。

    这套库里 140 个会话有 130 个是 2 轮的测试残留。不滤的话，每次跑都要为
    它们各做两次查询，而它们永远是空操作。
    """
    from app.memory.session_summary_worker import _list_candidate_sessions

    conn = await _conn()
    await _seed(conn, 11, session_id="够长")
    await _seed(conn, 8, session_id="刚好不够")
    await _seed(conn, 2, session_id="很短")

    candidates = await _list_candidate_sessions(conn, preserve_recent_messages=8)

    assert [s for _, s in candidates] == ["够长"]
    await conn.close()


async def test_already_covered_sessions_do_not_consume_the_limit():
    """limit 限制的是"更新了几个"。已经摘全的会话是空操作，让它们占掉名额
    会把真正要做的工作挤到下一次——而下一次它们还在前面。"""
    from app.memory.session_summary_worker import main

    conn = await _conn()
    await _seed(conn, 11, session_id="早就摘好的")
    await refresh_session_summary(
        conn, tenant_id="t1", session_id="早就摘好的",
        llm_registry=ScriptedLLM(["旧摘要"]), llm_provider_name="fake",
    )
    await _seed(conn, 11, session_id="还没摘的")  # 更晚活跃，排在候选列表前面

    updated = await main(memory_conn=conn, llm_registry=ScriptedLLM(["新摘要"]), limit=1)

    assert updated == 1
    stored = await get_session_summary(conn, tenant_id="t1", session_id="还没摘的")
    assert stored is not None and stored.summary == "新摘要"
    await conn.close()
