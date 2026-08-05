from app.graphrag.normalization import normalize_and_write_relations
from app.graphrag.ontology import Term

_TERMS = [
    Term(
        standard_name="错误码E502",
        aliases=["网关超时"],
        term_type="error_code",
        product_line="核心平台",
    ),
    Term(
        standard_name="登录模块",
        aliases=["认证模块"],
        term_type="module",
        product_line="核心平台",
    ),
]


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type
    ) -> None:
        if relation_type not in {"RELATED_TO", "BELONGS_TO_MODULE"}:
            raise ValueError("不允许的关系类型")
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
            }
        )


async def test_writes_relation_when_both_sides_resolve_via_alias():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"}
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "错误码E502",
            "object": "登录模块",
            "relation_type": "RELATED_TO",
        }
    ]


async def test_drops_relation_when_one_side_unresolved():
    graph_client = FakeGraphClient()
    relations = [
        {
            "subject": "网关超时",
            "object": "不存在的实体",
            "relation_type": "RELATED_TO",
        }
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client
    )

    assert written == 0
    assert graph_client.written == []


async def test_drops_relation_with_invalid_relation_type_without_crashing_batch():
    graph_client = FakeGraphClient()
    relations = [
        {"subject": "网关超时", "object": "认证模块", "relation_type": "非法类型"},
        {"subject": "网关超时", "object": "认证模块", "relation_type": "RELATED_TO"},
    ]

    written = await normalize_and_write_relations(
        relations, terms=_TERMS, graph_client=graph_client
    )

    assert written == 1
