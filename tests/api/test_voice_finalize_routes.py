from fastapi.testclient import TestClient

from app.api import deps
from app.graphrag.ontology import Term
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


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
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("clip.wav", b"fake-audio", "audio/wav")},
            # gateway_shared_secret 未配置（沿用本文件既有的默认 Settings，未 override
            # deps.get_settings），resolve_tenant_id() 会走 fallback 降级路径，因此这里
            # 显式带上 tenant_id query 参数，避免因缺少任何租户身份而被 422 拒绝——
            # 这条测试本身关注的是转写+专有名词校正逻辑，与租户鉴权无关。
            params={"tenant_id": "t1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"text": "我这边报了网关超时示例"}
