from app.graphrag.ontology import Term
from app.ingestion.graph_extraction import extract_and_write_graph_relations
from app.ingestion.chunking import Chunk
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry

_TERMS = [
    Term(
        standard_name="示例错误码E502",
        aliases=["网关超时示例"],
        term_type="error_code",
        product_line="示例产品线",
    ),
    Term(
        standard_name="示例登录模块",
        aliases=["示例认证模块"],
        term_type="module",
        product_line="示例产品线",
    ),
]


class FixedLLMProvider:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._text)


class FakeGraphClient:
    def __init__(self) -> None:
        self.written: list[dict] = []

    async def merge_relation(
        self, *, subject_standard_name, object_standard_name, relation_type
    ) -> None:
        self.written.append(
            {
                "subject": subject_standard_name,
                "object": object_standard_name,
                "relation_type": relation_type,
            }
        )


async def test_extracts_normalizes_and_writes_relations_from_chunks():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM,
        "llm",
        FixedLLMProvider(
            '{"relations": [{"subject": "网关超时示例", '
            '"object": "示例认证模块", "relation_type": "RELATED_TO"}]}'
        ),
    )
    graph_client = FakeGraphClient()
    chunks = [Chunk(text="网关超时示例通常与示例认证模块相关", heading_path=[], source="a.md")]

    written = await extract_and_write_graph_relations(
        chunks,
        llm_registry=llm_registry,
        llm_provider_name="llm",
        terms=_TERMS,
        graph_client=graph_client,
    )

    assert written == 1
    assert graph_client.written == [
        {
            "subject": "示例错误码E502",
            "object": "示例登录模块",
            "relation_type": "RELATED_TO",
        }
    ]
