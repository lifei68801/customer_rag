import json

import aiosqlite
from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology import Term
from app.graphrag.term_edits_store import ensure_term_edits_schema
from app.graphrag.terms_store import ensure_terms_schema
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry
from tests.settings_factory import build_settings


async def _override_get_review_conn_with_terms(terms: list[Term]) -> aiosqlite.Connection:
    # asr_finalize_endpoint 现在直接用 review_conn 查术语表（不再经过已删除
    # 的 deps.get_terms，见 app/api/deps.py 顶部说明）——这条测试的断言依赖
    # 具体的术语内容（专有名词校正要真的命中别名），所以直接往 terms 表插
    # 这条测试自己的术语，绕开 create_term() 的分类校验，跟
    # test_admin_graph_review_routes.py::_seed_terms 是同一个模式。
    conn = await aiosqlite.connect(":memory:")
    await ensure_terms_schema(conn)
    # Task 3：asr_finalize_endpoint 现在经 list_terms_merged() 读术语表，
    # 测试连接要把 term_edits 表也建好，否则会报 "no such table: term_edits"。
    await ensure_term_edits_schema(conn)
    for term in terms:
        await conn.execute(
            "INSERT OR REPLACE INTO terms "
            "(tenant_id, node_key, standard_name, aliases, term_type, "
            "extra_properties) VALUES (?, ?, ?, ?, ?, ?)",
            (
                term.tenant_id,
                term.node_key,
                term.standard_name,
                json.dumps(term.aliases, ensure_ascii=False),
                term.term_type,
                json.dumps(term.extra_properties, ensure_ascii=False),
            ),
        )
    await conn.commit()
    return conn


_settings = build_settings


class FakeASRProvider:
    async def transcribe(self, request: ASRRequest) -> ASRResult:
        return ASRResult(text="我这边报了网关超时是例")


class FixedLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(
            text=(
                '{"replacements": [{"span": "网关超时是例", '
                '"standard_name": "网关超时示例", "replace": true}]}'
            )
        )


def test_asr_finalize_transcribes_and_corrects_terms():
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FixedLLMProvider()
    )
    terms = [
        Term(
            # 请求走 tenant_id query 参数 "t1"（gateway_shared_secret 未配置，
            # resolve_tenant_id() 会用这个值查术语表），种子术语的 tenant_id
            # 必须跟它一致——不再是任意占位的 "default"，见下面 review_conn
            # 的说明。
            tenant_id="t1",
            node_key="示例错误码E502",
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
        )
    ]

    async def _review_conn_override() -> aiosqlite.Connection:
        return await _override_get_review_conn_with_terms(terms)

    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_review_conn] = _review_conn_override
    # gateway_shared_secret 显式钉死为 None：不 override 的话 get_settings
    # 会读真实环境变量/.env，一旦开发者本机或 .env 配置了
    # CUSTOMER_RAG_GATEWAY_SHARED_SECRET（正是这个安全修复要促使运营者去做
    # 的事），这条与租户鉴权无关的测试会意外因缺少网关凭证被 401 拒绝。
    app.dependency_overrides[deps.get_settings] = lambda: _settings()
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("clip.wav", b"fake-audio", "audio/wav")},
            # gateway_shared_secret 未配置，resolve_tenant_id() 会走 fallback
            # 降级路径，因此这里显式带上 tenant_id query 参数，避免因缺少任何
            # 租户身份而被 422 拒绝——这条测试本身关注的是转写+专有名词校正
            # 逻辑，与租户鉴权无关。
            params={"tenant_id": "t1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"text": "我这边报了网关超时示例"}
