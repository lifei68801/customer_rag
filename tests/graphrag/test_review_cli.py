import aiosqlite

from app.graphrag.ontology import Term
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.review_cli import cmd_approve, cmd_list, cmd_reject
from app.graphrag.review_queue import enqueue_for_review, ensure_review_schema, list_pending_reviews


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_review_schema(conn)
    # cmd_approve 现在还会查该租户 status="confirmed" 的关系类型/类型组合
    # 白名单（Fix 1：approve_review 补齐了跟 normalize_and_write_relations
    # 一样的"已确认本体范围"校验），这两张表也要建好，否则查询会报
    # "no such table"。
    await ensure_ontology_schema(conn)
    return conn


async def _seed_confirmed_ontology(
    conn: aiosqlite.Connection, *, tenant_id: str, relation_type: str,
    subject_term_type: str, object_term_type: str,
) -> None:
    """直接往 tenant_relation_types/term_type_relation_allowlist 插入
    status='confirmed' 的行，供 cmd_approve 校验通过——测试不需要走完整的
    草稿编辑+confirm_ontology 生命周期。"""
    await conn.execute(
        "INSERT INTO tenant_relation_types "
        "(tenant_id, relation_type, example_phrase, description, allow_chain_query, "
        "source, status) VALUES (?, ?, ?, '', 0, 'custom', 'confirmed')",
        (tenant_id, relation_type, relation_type),
    )
    await conn.execute(
        "INSERT INTO term_type_relation_allowlist "
        "(tenant_id, subject_term_type, relation_type, object_term_type, status) "
        "VALUES (?, ?, ?, ?, 'confirmed')",
        (tenant_id, subject_term_type, relation_type, object_term_type),
    )
    await conn.commit()


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self,
        *,
        subject_standard_name,
        object_standard_name,
        relation_type,
        source,
        tenant_id,
        provenance,
        recorded_at,
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
                "source": source,
                "tenant_id": tenant_id,
                "provenance": provenance,
                "recorded_at": recorded_at,
            }
        )


async def test_cmd_list_returns_pending_rows():
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    pending = await cmd_list(review_conn=conn, tenant_id="t1")

    assert len(pending) == 1
    assert pending[0]["subject_candidate"] == "a"


async def test_cmd_list_does_not_leak_another_tenant():
    conn = await _connect()
    await enqueue_for_review(
        conn, subject_candidate="a", object_candidate="b", relation_type="RELATED_TO",
        reason="subject_unresolved", source="s.md", tenant_id="t1",
    )

    assert await cmd_list(review_conn=conn, tenant_id="t2") == []


async def test_cmd_approve_writes_relation_via_graph_client():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="网关超时示例2.0",
        object_candidate="认证模块示例",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="faq.md",
        tenant_id="t1",
    )
    graph_client = FakeGraphClient()
    await _seed_confirmed_ontology(
        conn, tenant_id="t1", relation_type="RELATED_TO",
        subject_term_type="", object_term_type="",
    )

    await cmd_approve(
        review_conn=conn,
        review_id=review_id,
        subject_standard_name="示例错误码E502",
        object_standard_name="示例登录模块",
        tenant_id="t1",
        graph_client=graph_client,
        terms=[
            Term(
                tenant_id="t1", node_key="示例错误码E502",
                standard_name="示例错误码E502", aliases=[],
                term_type="", product_line="",
            ),
            Term(
                tenant_id="t1", node_key="示例登录模块",
                standard_name="示例登录模块", aliases=[],
                term_type="", product_line="",
            ),
        ],
    )

    # cmd_approve 内部用 datetime.now() 生成 recorded_at，测试跑的时刻不可
    # 预知具体值，只断言其它字段+provenance（这是 human_approved 路径，
    # 不是自动写入）。
    assert len(graph_client.written) == 1
    written = graph_client.written[0]
    assert written["subject"] == "示例错误码E502"
    assert written["object"] == "示例登录模块"
    assert written["relation_type"] == "RELATED_TO"
    assert written["source"] == "faq.md"
    assert written["tenant_id"] == "t1"
    assert written["provenance"] == "human_approved"
    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_cmd_reject_removes_from_pending():
    conn = await _connect()
    review_id = await enqueue_for_review(
        conn,
        subject_candidate="噪声实体",
        object_candidate="另一个噪声",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await cmd_reject(review_conn=conn, review_id=review_id, tenant_id="t1", note="确认是噪声")

    assert await list_pending_reviews(conn, tenant_id="t1") == []


async def test_cmd_list_prints_suggested_standard_names_when_present(capsys):
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="网关超时了",
        object_candidate="认证模块",
        relation_type="RELATED_TO",
        reason="fuzzy_match_needs_confirmation",
        source="s.md",
        tenant_id="t1",
        suggested_subject_standard_name="错误码E502",
        suggested_object_standard_name=None,
    )

    await cmd_list(review_conn=conn, tenant_id="t1")

    captured = capsys.readouterr()
    assert "建议" in captured.out
    assert "subject→错误码E502" in captured.out


async def test_cmd_list_does_not_print_suggestion_section_when_absent(capsys):
    conn = await _connect()
    await enqueue_for_review(
        conn,
        subject_candidate="a",
        object_candidate="b",
        relation_type="RELATED_TO",
        reason="subject_unresolved",
        source="s.md",
        tenant_id="t1",
    )

    await cmd_list(review_conn=conn, tenant_id="t1")

    captured = capsys.readouterr()
    assert "建议" not in captured.out
