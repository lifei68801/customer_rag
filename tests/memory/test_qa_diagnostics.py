import json

import aiosqlite

from app.memory.qa_diagnostics import (
    CONTENT_LIMIT,
    RETENTION_PER_TENANT,
    list_diagnostics,
    get_diagnostic,
    record_diagnostic,
)
from app.memory.schema import ensure_schema

# 问答诊断的快照。
#
# 「答错了」反查实体是这个项目里发现数据问题的主路径，但当时用了哪些工具、
# 匹配到哪些实体，此前只活在内存里，一轮对话结束就没了。重跑不能替代——
# LLM 非确定性，可能复现不出那个错误，你会对着一个正确结果找不到问题。


async def _connect():
    conn = await aiosqlite.connect(":memory:")
    await ensure_schema(conn)
    return conn


def _tool_result(name="structured_filter_query_tool", content=None):
    return {
        "tool_call_id": "call_1",
        "name": name,
        "content": json.dumps(content or {"anchors": [{"node_key": "公司:可口可乐"}]}, ensure_ascii=False),
    }


async def _record(conn, **over):
    payload = {
        "tenant_id": "t1",
        "session_id": "s1",
        "question": "可口可乐有哪些产品",
        "resolved_question": "可口可乐有哪些产品",
        "answer": "雪碧、芬达。",
        "used_sources": ["doc-1"],
        "tool_results": [_tool_result()],
    }
    payload.update(over)
    return await record_diagnostic(conn, **payload)


async def test_records_and_reads_back_the_snapshot():
    conn = await _connect()

    await _record(conn)

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    assert len(rows) == 1
    detail = await get_diagnostic(conn, tenant_id="t1", diagnostic_id=rows[0]["id"])
    assert detail["question"] == "可口可乐有哪些产品"
    assert detail["answer"] == "雪碧、芬达。"
    assert detail["used_sources"] == ["doc-1"]
    assert detail["tool_results"][0]["name"] == "structured_filter_query_tool"


async def test_oversized_content_is_truncated_and_flagged():
    """structured_filter_query 命中上千条时 content 会很大。截断而不是丢弃：
    工具名、参数、前面那部分结果通常已经够定位问题；但必须标出来，否则
    排查的人会以为「结果就这么多」，据此得出错误结论。"""
    conn = await _connect()
    huge = {"rows": ["x" * 100 for _ in range(500)]}

    await _record(conn, tool_results=[_tool_result(content=huge)])

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    detail = await get_diagnostic(conn, tenant_id="t1", diagnostic_id=rows[0]["id"])
    saved = detail["tool_results"][0]
    assert len(saved["content"]) <= CONTENT_LIMIT
    assert saved["content_truncated"] is True


async def test_normal_content_is_not_flagged():
    """没被截断的不该带这个标记——排查时看到 truncated 就得怀疑自己看到的
    是不是全部，每条都带等于这个信号失效。"""
    conn = await _connect()

    await _record(conn)

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    detail = await get_diagnostic(conn, tenant_id="t1", diagnostic_id=rows[0]["id"])
    assert detail["tool_results"][0].get("content_truncated") is not True


async def test_old_records_are_dropped_beyond_retention():
    """无上限增长会撑爆内存库。保留最近 N 条——诊断的对象是「最近答错的
    那次」，三个月前的问答已经无从对照当时的数据了。"""
    conn = await _connect()
    for i in range(RETENTION_PER_TENANT + 5):
        await _record(conn, question=f"问题{i}")

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1", limit=None)
    assert len(rows) == RETENTION_PER_TENANT
    # 留下的是最近的那批。
    assert rows[0]["question"] == f"问题{RETENTION_PER_TENANT + 4}"


async def test_retention_is_per_tenant():
    """一个高频租户不该把别人的诊断记录挤掉。"""
    conn = await _connect()
    for i in range(RETENTION_PER_TENANT + 5):
        await _record(conn, question=f"忙碌租户{i}")
    await _record(conn, tenant_id="t2", question="安静租户")

    assert len(await list_diagnostics(conn, tenant_id="t2", session_id="s1")) == 1


async def test_other_tenants_are_invisible():
    conn = await _connect()
    await _record(conn, tenant_id="t1")
    await _record(conn, tenant_id="t2")

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    assert len(rows) == 1
    detail = await get_diagnostic(conn, tenant_id="t2", diagnostic_id=rows[0]["id"])
    assert detail is None, "拿别的租户的 id 应该查不到"


async def test_listing_is_newest_first():
    """诊断的入口是「刚才那次答错了」，最近的排最前面。"""
    conn = await _connect()
    await _record(conn, question="第一次")
    await _record(conn, question="第二次")

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    assert [r["question"] for r in rows] == ["第二次", "第一次"]


async def test_a_normal_answer_is_recorded_as_answered():
    """不传 outcome 时默认 answered。

    默认成 no_match 的话，报错明细上线第一天就会显示历史上每一次问答都失败了。
    """
    conn = await _connect()

    diagnostic_id = await _record(conn)

    detail = await get_diagnostic(conn, tenant_id="t1", diagnostic_id=diagnostic_id)
    assert detail["outcome"] == "answered"
    await conn.close()


async def test_the_outcome_is_stored_as_given():
    """三个取值都能存进去、读得回来。

    只测 answered 的话，一个把参数丢掉、恒写 'answered' 的实现照样绿。
    """
    conn = await _connect()

    for outcome in ("answered", "no_match", "error"):
        diagnostic_id = await _record(conn, outcome=outcome)
        detail = await get_diagnostic(conn, tenant_id="t1", diagnostic_id=diagnostic_id)
        assert detail["outcome"] == outcome
    await conn.close()


async def test_the_list_carries_the_outcome():
    """列表页要按 outcome 分「未命中 / 报错」两个页签，所以列表就得带上它。

    只在详情里带的话，列表得为每一行再查一次详情才知道该放进哪个页签。
    """
    conn = await _connect()

    await _record(conn, outcome="no_match")

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    assert [row["outcome"] for row in rows] == ["no_match"]
    await conn.close()


async def test_existing_rows_read_back_as_answered():
    """加列之前的历史记录没有这一列。

    DEFAULT 'answered' 让它们读回来是「答过」——那是最接近事实的假设
    （它们当时确实产出了答案）。读成 no_match 的话，报错明细第一屏全是
    上线前的历史记录，真正的问题被埋在下面。
    """
    conn = await aiosqlite.connect(":memory:")
    # 手工造一张"加列之前"的表，再让 ensure_schema 去迁移它。直接 ensure_schema
    # 建出来的新表本来就有这一列，测不到迁移这条路径。
    await conn.execute(
        "CREATE TABLE qa_diagnostics ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,"
        " session_id TEXT NOT NULL, question TEXT NOT NULL, resolved_question TEXT,"
        " answer TEXT NOT NULL, used_sources TEXT NOT NULL, tool_results TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    await conn.execute(
        "INSERT INTO qa_diagnostics (tenant_id, session_id, question, answer,"
        " used_sources, tool_results) VALUES ('t1', 's1', '老问题', '老答案', '[]', '[]')"
    )
    await conn.commit()

    await ensure_schema(conn)

    rows = await list_diagnostics(conn, tenant_id="t1", session_id="s1")
    assert [row["outcome"] for row in rows] == ["answered"]
    await conn.close()
