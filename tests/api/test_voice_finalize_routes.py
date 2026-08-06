from fastapi.testclient import TestClient

from app.api import deps
from app.config.settings import Settings
from app.graphrag.ontology import Term
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


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
            standard_name="示例错误码E502",
            aliases=["网关超时示例"],
            term_type="error_code",
            product_line="示例产品线",
        )
    ]

    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_terms] = lambda: terms
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
