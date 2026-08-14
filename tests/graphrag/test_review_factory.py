from app.config.settings import Settings
from app.graphrag.ontology_lifecycle import checkout_draft
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.review_factory import build_review_conn_from_settings
from app.graphrag.review_queue import enqueue_for_review, list_pending_reviews


def _settings(db_path) -> Settings:
    return Settings(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=1024,
        graph_review_db_path=str(db_path),
    )


async def test_build_review_conn_from_settings_creates_usable_schema(tmp_path):
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = _settings(db_path)

    conn = await build_review_conn_from_settings(settings)
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="test.md",
        tenant_id="demo",
    )
    pending = await list_pending_reviews(conn, tenant_id="demo")

    assert len(pending) == 1
    assert db_path.exists()


async def test_build_review_conn_from_settings_creates_ontology_tables(tmp_path):
    """回归测试：ensure_ontology_schema（Task 4 的统一本体建表入口，建
    tenant_relation_types/term_type_relation_allowlist 两张表）必须在这个
    连接工厂里被实际调用，不能只在测试的 dependency_overrides 里手动补——
    否则 Task 7 新增的关系类型/约束/生命周期路由和 store 函数在真实环境下
    第一次被访问就会因为 "no such table" 报 500（见本仓库对该缺口的
    review 记录）。这里不用 dependency_overrides 绕过 schema 建表，而是
    走真实的 build_review_conn_from_settings 调用链，直接验证两张表已经
    建好、且可以正常读写——checkout_draft 是 Task 4 生命周期编排的入口，
    会往 tenant_relation_types 写入 10 条默认关系类型，list_relation_types
    读不出来就说明表没建对。
    """
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = _settings(db_path)

    conn = await build_review_conn_from_settings(settings)
    await checkout_draft(conn, "demo-tenant")
    relation_types = await list_relation_types(conn, "demo-tenant", status="draft")

    assert len(relation_types) == 10
