from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSessionStore
from app.memory.qa_diagnostics import record_diagnostic
from app.memory.schema import ensure_schema
from app.main import app
from tests.settings_factory import build_settings

# 问答诊断的读取接口。
#
# 反查的路径是「这次答错了」→「用了哪些工具」→「匹配到哪些实体」→ 实体
# 详情页。中间那一跳需要把 tool_results 里散落的 node_key 抽出来——它们
# 藏在 anchors / candidates / neighbors 三种不同结构里，让前端各解析一遍
# 只会解析漏。


def _settings(**overrides):
    return build_settings(**{"admin_token": "tok", **overrides})


@pytest.fixture
def memory_conn():
    """必须显式 close：aiosqlite 的后台线程不是 daemon，泄漏连接会让 pytest
    跑完全部用例后卡在解释器退出阶段。"""
    async def _open():
        conn = await aiosqlite.connect(":memory:")
        await ensure_schema(conn)
        return conn

    conn = asyncio.run(_open())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _tool(name, observation):
    return {
        "tool_call_id": "c1",
        "name": name,
        "content": json.dumps(observation, ensure_ascii=False),
    }


async def _seed(conn, *, tenant_id="demo", tool_results=None, **over):
    payload = {
        "tenant_id": tenant_id,
        "session_id": "s1",
        "question": "可口可乐有哪些产品",
        "resolved_question": "可口可乐有哪些产品",
        "answer": "雪碧、芬达。",
        "used_sources": ["doc-1"],
        "tool_results": tool_results if tool_results is not None else [],
    }
    payload.update(over)
    return await record_diagnostic(conn, **payload)


def _call(memory_conn, path: str):
    session_store = AdminSessionStore()
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    app.dependency_overrides[deps.get_admin_session_store] = lambda: session_store
    app.dependency_overrides[deps.get_memory_conn] = lambda: memory_conn
    try:
        client = TestClient(app)
        token = session_store.create_session(username="admin", role="admin", tenant_id=None)
        return client.get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        app.dependency_overrides.clear()


def test_lists_recent_diagnostics_newest_first(memory_conn):
    asyncio.run(_seed(memory_conn, question="第一次"))
    asyncio.run(_seed(memory_conn, question="第二次"))

    body = _call(memory_conn, "/api/admin/demo/diagnostics").json()

    assert [d["question"] for d in body["diagnostics"]] == ["第二次", "第一次"]
    # 列表不带 tool_results：那是详情才需要的大字段，列进来每次翻页都要
    # 传上几百 KB。
    assert "tool_results" not in body["diagnostics"][0]


def test_detail_extracts_mentioned_terms_from_every_shape(memory_conn):
    """node_key 藏在 anchors / candidates / neighbors 三种结构里。让前端
    各解析一遍只会解析漏——漏掉的表现是「这个实体明明被用到了却没列出来」，
    而排查的人会据此排除掉真正的元凶。"""
    diag_id = asyncio.run(_seed(memory_conn, tool_results=[
        _tool("structured_filter_query_tool", {
            "matched_count": 1,
            "anchors": [
                {
                    "node_key": "公司:可口可乐", "standard_name": "可口可乐", "term_type": "公司",
                    "neighbors": [
                        {"node_key": "产品:雪碧", "standard_name": "雪碧", "term_type": "产品"},
                    ],
                },
            ],
        }),
        _tool("another_tool", {
            "ambiguous_anchor": {
                "name": "可乐",
                "candidates": [
                    {"node_key": "公司:百事可乐", "standard_name": "百事可乐", "term_type": "公司"},
                ],
            }
        }),
    ]))

    body = _call(memory_conn, f"/api/admin/demo/diagnostics/{diag_id}").json()

    keys = {t["node_key"] for t in body["mentioned_terms"]}
    assert keys == {"公司:可口可乐", "产品:雪碧", "公司:百事可乐"}


def test_mentioned_terms_are_deduplicated(memory_conn):
    """同一个实体在多轮工具调用里反复出现是常态。列三遍只会让人以为
    它被用了三次。"""
    diag_id = asyncio.run(_seed(memory_conn, tool_results=[
        _tool("t1", {"anchors": [{"node_key": "公司:可口可乐", "standard_name": "可口可乐"}]}),
        _tool("t2", {"anchors": [{"node_key": "公司:可口可乐", "standard_name": "可口可乐"}]}),
    ]))

    body = _call(memory_conn, f"/api/admin/demo/diagnostics/{diag_id}").json()

    assert len(body["mentioned_terms"]) == 1


def test_no_terms_mentioned_is_a_real_answer(memory_conn):
    """一次问答完全没命中任何实体，本身就是重要线索——说明它走的是纯向量
    检索，图谱一点没用上。返回空数组，别让前端分不清「没有」和「没解析」。"""
    diag_id = asyncio.run(_seed(memory_conn, tool_results=[
        _tool("vector_search_tool", {"records": [{"id": "doc-1"}]}),
    ]))

    body = _call(memory_conn, f"/api/admin/demo/diagnostics/{diag_id}").json()

    assert body["mentioned_terms"] == []


def test_malformed_tool_content_does_not_break_the_page(memory_conn):
    """content 不是合法 JSON（工具报错时会写别的东西）也要能打开诊断页——
    正是出错那次最需要看。"""
    diag_id = asyncio.run(_seed(memory_conn, tool_results=[
        {"tool_call_id": "c1", "name": "broken", "content": "not json at all"},
    ]))

    response = _call(memory_conn, f"/api/admin/demo/diagnostics/{diag_id}")

    assert response.status_code == 200
    assert response.json()["mentioned_terms"] == []


def test_other_tenants_diagnostics_are_invisible(memory_conn):
    diag_id = asyncio.run(_seed(memory_conn, tenant_id="other"))

    assert _call(memory_conn, f"/api/admin/demo/diagnostics/{diag_id}").status_code == 404
