from app.graphrag.ontology_lifecycle import checkout_draft
from app.graphrag.ontology_relations import list_relation_types
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.review_queue import enqueue_for_review, list_pending_reviews
from tests.settings_factory import build_settings


def _settings(db_path):
    return build_settings(graph_review_db_path=str(db_path))


_ONTOLOGY_STORE_TABLES = frozenset({
    # 期望名单在这里独立硬写，不从被测模块的常量读回来——从常量读等于让实现
    # 自我印证，表漏建时测试会跟着一起漏。
    "terms",
    "ontology_term_types",
    "ontology_draft_checkout_state",
    "tenant_relation_types",
    "term_type_relation_allowlist",
    "tenant_ingestion_config",
    "graph_review_queue",
    "duplicate_review_queue",
    "etl_runs",
    "tenants",
})


async def test_ontology_store_conn_creates_every_table(tmp_path):
    """本体库连接必须一次建齐全部十张表。

    这个 seam 此前没有任何测试：走 API 的测试一律用
    app.dependency_overrides 把 deps.get_review_conn 整个替换掉，其余 64 个
    测试文件各自开 :memory: 连接、只调自己需要的 ensure_*。生产的建表清单
    从不被套件执行，所以 deps.py（7 个 ensure_*）和这个工厂（3 个）的分叉
    才能一路悄悄扩大，"no such table" 只能在生产被发现——本文件里那条
    ensure_ontology_schema 的回归测试就是上一次事故留下的。

    etl_stable_code_registry 不在名单里：它由 schema_etl 自己建，不属于
    连接契约。
    """
    conn = await open_ontology_store_conn(_settings(tmp_path / "ontology_store.sqlite3"))
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in await cursor.fetchall()}

    assert _ONTOLOGY_STORE_TABLES <= names, f"缺表: {sorted(_ONTOLOGY_STORE_TABLES - names)}"


async def test_ontology_store_conn_creates_usable_schema(tmp_path):
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = _settings(db_path)

    conn = await open_ontology_store_conn(settings)
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


async def test_ontology_store_conn_creates_ontology_tables(tmp_path):
    """回归测试：ensure_ontology_schema（Task 4 的统一本体建表入口，建
    tenant_relation_types/term_type_relation_allowlist 两张表）必须在这个
    连接工厂里被实际调用，不能只在测试的 dependency_overrides 里手动补——
    否则 Task 7 新增的关系类型/约束/生命周期路由和 store 函数在真实环境下
    第一次被访问就会因为 "no such table" 报 500（见本仓库对该缺口的
    review 记录）。这里不用 dependency_overrides 绕过 schema 建表，而是
    走真实的 open_ontology_store_conn 调用链，直接验证两张表已经
    建好、且可以正常读写——checkout_draft 是 Task 4 生命周期编排的入口，
    会往 tenant_relation_types 写入 10 条默认关系类型，list_relation_types
    读不出来就说明表没建对。
    """
    db_path = tmp_path / "graph_review_queue.sqlite3"
    settings = _settings(db_path)

    conn = await open_ontology_store_conn(settings)
    await checkout_draft(conn, "demo-tenant")
    relation_types = await list_relation_types(conn, "demo-tenant", status="draft")

    assert len(relation_types) == 10
