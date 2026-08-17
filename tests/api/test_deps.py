import asyncio
import logging

import aiosqlite
import pytest
from fastapi import HTTPException

from app.api import deps
from app.config.settings import Settings
from app.graphrag.ontology_lifecycle import checkout_draft, ensure_ontology_schema
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_queue import ensure_review_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.graphrag.tenant_ingestion_config import set_ingestion_mode


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
        gateway_shared_secret=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_get_gateway_tenant_id_returns_none_when_secret_not_configured():
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret=None, settings=_settings()
    )
    assert result is None


async def test_get_gateway_tenant_id_returns_header_value_when_secret_matches():
    settings = _settings(gateway_shared_secret="sekret")
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret="sekret", settings=settings
    )
    assert result == "t1"


async def test_get_gateway_tenant_id_rejects_when_secret_mismatches():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id="t1", x_gateway_secret="wrong", settings=settings
        )
    assert exc_info.value.status_code == 401


async def test_get_gateway_tenant_id_rejects_when_tenant_header_missing():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id=None, x_gateway_secret="sekret", settings=settings
        )
    assert exc_info.value.status_code == 401


def test_resolve_tenant_id_prefers_gateway_value():
    result = deps.resolve_tenant_id("t1", "t2", source="test")
    assert result == "t1"


def test_resolve_tenant_id_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        result = deps.resolve_tenant_id(None, "t2", source="test")
    assert result == "t2"
    assert any("t2" in record.message for record in caplog.records)


def test_resolve_tenant_id_raises_when_both_missing():
    with pytest.raises(HTTPException) as exc_info:
        deps.resolve_tenant_id(None, None, source="test")
    assert exc_info.value.status_code == 422


def test_parse_banned_terms_returns_none_when_unset():
    assert deps.parse_banned_terms(None) is None


def test_parse_banned_terms_returns_none_when_empty_string():
    assert deps.parse_banned_terms("") is None


def test_parse_banned_terms_splits_comma_separated_values():
    assert deps.parse_banned_terms("敏感词1,敏感词2,敏感词3") == [
        "敏感词1",
        "敏感词2",
        "敏感词3",
    ]


def test_parse_banned_terms_strips_whitespace_around_each_term():
    assert deps.parse_banned_terms(" 敏感词1 , 敏感词2 ") == ["敏感词1", "敏感词2"]


async def test_get_graph_client_calls_ensure_tenant_scoped_schema(monkeypatch):
    """走真实的 get_graph_client 依赖链（不用 dependency_overrides 绕过），
    验证单例第一次构建时会调用 ensure_tenant_scoped_schema——这是 Task 3
    新建索引/回填存量节点唯一会被执行到的地方，漏接线的话索引永远不会
    被创建，全表扫描问题在真实环境里一直存在。
    """
    import app.api.deps as deps_module

    monkeypatch.setattr(deps_module, "_graph_client_cache", None)
    calls: list[str] = []

    class _FakeGraphClient:
        async def ensure_tenant_scoped_schema(self) -> None:
            calls.append("ensure_tenant_scoped_schema")

    monkeypatch.setattr(
        deps_module, "build_graph_client_from_settings", lambda settings: _FakeGraphClient()
    )

    await deps_module.get_graph_client(settings=_settings())

    assert calls == ["ensure_tenant_scoped_schema"]


async def test_get_review_conn_creates_ontology_tables(tmp_path, monkeypatch):
    """回归测试：get_review_conn（Task 7 的关系类型/约束/生命周期路由实际依赖
    的连接来源）必须真的建出 tenant_relation_types/term_type_relation_allowlist
    两张表，不能只在测试的 dependency_overrides 里手动补——路由测试一律通过
    app.dependency_overrides[deps.get_review_conn] 换成手工建表的连接，那条
    路径测不出 get_review_conn 自身的建表调用是否遗漏了 Task 4 的
    ensure_ontology_schema（真实发生过的缺口：见本仓库对该问题的 review
    记录）。这里绕开 FastAPI 的 Depends 包装，直接调用 get_review_conn 本体，
    走真实的建表链路。

    get_review_conn 内部用了一个进程级单例缓存（_review_conn_cache）——这里
    在测试前后都重置它，避免这个测试跑之后缓存住一个指向 tmp_path 的连接，
    污染同一个 pytest 进程里其它（未来可能新增的、不走 dependency_overrides
    的）测试。
    """
    monkeypatch.setattr(deps, "_review_conn_cache", None)
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = _settings(
        graph_review_db_path=str(db_path),
        terminology_path=str(tmp_path / "no_such_seed.yaml"),
    )

    conn = await deps.get_review_conn(settings=settings)
    try:
        await checkout_draft(conn, "demo-tenant")
        relation_types = await list_relation_types(conn, "demo-tenant", status="draft")
        assert len(relation_types) == 10
    finally:
        await conn.close()
        monkeypatch.setattr(deps, "_review_conn_cache", None)


async def _open_review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    await ensure_terms_schema(conn)
    # get_confirmed_relation_types/get_term_type_schema 依赖的是 get_review_conn
    # 实际建出的连接，那个连接同时建审核队列/terms/本体（分类+关系类型+约束）三套
    # schema——这里照抄同样的建表顺序，不然 checkout_draft/create_relation_type/
    # create_term_type 会因为表不存在而报错。
    await ensure_ontology_schema(conn)
    return conn


@pytest.fixture
def review_conn():
    """本体（分类/关系类型）测试用连接。close 的理由同本文件其它 review_conn
    风格的 fixture（见 test_admin_document_routes.py/test_admin_graph_review_routes.py
    的同名 fixture 说明：aiosqlite 后台工作线程不是 daemon 线程，不显式 close
    会让 pytest 进程卡在解释器退出阶段）。"""
    conn = asyncio.run(_open_review_conn())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


async def test_get_confirmed_relation_types_returns_only_confirmed_types(review_conn):
    from app.graphrag.ontology_lifecycle import confirm_ontology
    from app.graphrag.ontology_relations import create_relation_type

    # 新租户的接入模式默认是 "extraction"（tenant_ingestion_config.get_ingestion_mode
    # 的默认值），checkout_draft 在这种模式下会自动播种 10 种全局默认关系类型到草稿——
    # 与本用例想验证的东西（get_confirmed_relation_types 只返回已确认类型、不受草稿
    # 干扰）无关，会污染断言。显式切到 "etl" 模式关掉这个播种，让草稿只有本用例自己
    # 创建的这一个类型，跟 get_term_type_schema 测试一样保持结果可预测。
    await set_ingestion_mode(review_conn, "muji", "etl")
    await checkout_draft(review_conn, "muji")
    await create_relation_type(review_conn, "muji", relation_type="HAS_SKU", example_phrase="p")
    await confirm_ontology(review_conn, "muji")

    result = await deps.get_confirmed_relation_types(
        review_conn=review_conn, gateway_tenant_id="muji",
    )

    assert result == {"HAS_SKU"}


async def test_get_term_type_schema_returns_dict_keyed_by_value(review_conn):
    from app.graphrag.ontology_categories import create_term_type

    await create_term_type(review_conn, tenant_id="muji", value="SKU")

    result = await deps.get_term_type_schema(review_conn=review_conn, gateway_tenant_id="muji")

    assert "SKU" in result
    assert result["SKU"].value == "SKU"
