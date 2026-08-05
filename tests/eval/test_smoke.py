"""CI 冒烟测试：确认随仓库发布的占位评测集本身能被完整跑通。

区别于 test_runner.py/test_eval_main.py —— 那两个用内联的合成数据验证
run_eval_suite() 的逻辑；这个测试专门加载真正会被打包发布的
app/eval/eval_seed.jsonl，防止有人改坏这个文件的 JSONL 格式或改动
EvalCase 字段却忘了同步更新种子文件，而单测本身察觉不到。
"""

from pathlib import Path

from app.eval.dataset import load_eval_cases
from app.eval.runner import run_eval_suite
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord

SEED_PATH = Path(__file__).resolve().parent.parent.parent / "app" / "eval" / "eval_seed.jsonl"


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class ScriptedLLMProvider:
    """每条用例依次消费：回答生成、faithfulness打分、answer_relevancy打分。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text=self._responses.pop(0))


async def test_eval_seed_dataset_runs_end_to_end_with_fake_providers():
    cases = load_eval_cases(SEED_PATH)
    assert len(cases) == 2  # 占位数据集应有 2 条；跌到 0 说明文件被改坏了

    records = [
        VectorRecord(
            id="faq/network.md",
            vector=[1.0, 0.0],
            text="网络断开时请先重启路由器。",
            tenant_id="t1",
            metadata={},
        ),
        VectorRecord(
            id="faq/login.md",
            vector=[1.0, 0.0],
            text="登录失败请检查账号密码是否正确，或使用找回密码功能。",
            tenant_id="t1",
            metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())

    scripted_responses: list[str] = []
    for _ in cases:
        scripted_responses.extend(
            ["按资料所述处理即可。", '{"score": 1.0}', '{"score": 1.0}']
        )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, "fake-llm", ScriptedLLMProvider(scripted_responses)
    )

    report = await run_eval_suite(
        cases,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name="fake-llm",
        tenant_id="t1",
        query_rewrite_enabled=False,
    )

    assert len(report.case_results) == 2
    assert report.average_context_recall == 1.0
    assert report.average_faithfulness == 1.0
    assert report.average_answer_relevancy == 1.0
