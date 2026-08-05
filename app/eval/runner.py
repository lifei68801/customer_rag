from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.eval.dataset import EvalCase, load_eval_cases
from app.eval.llm_judged_metrics import score_answer_relevancy, score_faithfulness
from app.eval.metrics import score_context_recall
from app.eval.terminology_accuracy import score_terminology_accuracy
from app.graphrag.factory import build_graph_client_from_settings, load_terms_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.term_guard import GraphClientProtocol
from app.providers.embedding import EmbeddingRegistry
from app.providers.factory import (
    DEFAULT_EMBEDDING_PROVIDER_NAME,
    DEFAULT_LLM_PROVIDER_NAME,
    build_embedding_registry_from_settings,
    build_llm_registry_from_settings,
)
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.providers.rerank_factory import build_rerank_provider_from_settings
from app.qa.answer import answer_question
from app.retrieval.bm25 import BM25Index, build_bm25_index_from_store
from app.retrieval.factory import build_vector_store_from_settings
from app.retrieval.vector_store import VectorStore


@dataclass(frozen=True)
class EvalCaseResult:
    question: str
    answer: str
    context_recall: float
    faithfulness: float | None
    answer_relevancy: float | None
    terminology_accuracy: float | None


@dataclass(frozen=True)
class EvalReport:
    case_results: list[EvalCaseResult]
    average_context_recall: float
    average_faithfulness: float | None
    average_answer_relevancy: float | None
    average_terminology_accuracy: float | None


def _average(values: list[float | None]) -> float | None:
    """跳过 None（未评出分数的用例），只对真正评出分数的用例求平均。"""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


async def run_eval_suite(
    cases: list[EvalCase],
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    top_k: int = 3,
) -> EvalReport:
    """对评测集里的每个用例真正跑一遍检索+生成，汇总 RAGAS 类指标。

    Faithfulness/Answer Relevancy 是 LLM 裁判打分，会真实调用一次 LLM
    （非 answer_question 生成阶段那次），因此跑一个完整评测集的耗时和
    成本是 O(2N) 次 LLM 调用（N=用例数），比只做确定性指标贵得多。
    """
    case_results: list[EvalCaseResult] = []
    for case in cases:
        result = await answer_question(
            case.question,
            embedding_registry=embedding_registry,
            embedding_provider_name=embedding_provider_name,
            vector_store=vector_store,
            bm25_index=bm25_index,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            top_k=top_k,
        )

        context_recall = score_context_recall(case, result.used_sources)
        faithfulness = await score_faithfulness(
            answer=result.text,
            context=result.retrieved_context,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        answer_relevancy = await score_answer_relevancy(
            question=case.question,
            answer=result.text,
            llm_registry=llm_registry,
            llm_provider_name=llm_provider_name,
        )
        terminology_accuracy = (
            score_terminology_accuracy(result.text, terms) if terms else None
        )

        case_results.append(
            EvalCaseResult(
                question=case.question,
                answer=result.text,
                context_recall=context_recall,
                faithfulness=faithfulness,
                answer_relevancy=answer_relevancy,
                terminology_accuracy=terminology_accuracy,
            )
        )

    return EvalReport(
        case_results=case_results,
        average_context_recall=_average(
            [r.context_recall for r in case_results]
        )
        or 0.0,
        average_faithfulness=_average([r.faithfulness for r in case_results]),
        average_answer_relevancy=_average(
            [r.answer_relevancy for r in case_results]
        ),
        average_terminology_accuracy=_average(
            [r.terminology_accuracy for r in case_results]
        ),
    )


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "N/A（无有效评分）"


async def main(
    *,
    dataset_path: Path,
    settings: Settings | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
    bm25_index: BM25Index | None = None,
    llm_registry: ProviderRegistry | None = None,
    rerank_provider: RerankProvider | None = None,
    terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
    use_graph: bool = False,
) -> EvalReport:
    """评测集运行脚本入口：加载 JSONL 评测集，跑一遍检索+生成，输出 RAGAS 类指标汇总。

    用法：
      python -m app.eval.runner --dataset app/eval/eval_seed.jsonl
      python -m app.eval.runner --dataset app/eval/eval_seed.jsonl --use-graph
    """
    resolved_settings = settings or Settings()
    cases = load_eval_cases(dataset_path)

    registry = embedding_registry or build_embedding_registry_from_settings(
        resolved_settings
    )
    store = vector_store or build_vector_store_from_settings(resolved_settings)
    index = bm25_index or await build_bm25_index_from_store(store)
    llm = llm_registry or build_llm_registry_from_settings(resolved_settings)
    rerank = (
        rerank_provider
        if rerank_provider is not None
        else build_rerank_provider_from_settings(resolved_settings)
    )

    resolved_terms = None
    resolved_graph_client = None
    if use_graph:
        resolved_terms = terms or load_terms_from_settings(resolved_settings)
        resolved_graph_client = graph_client or build_graph_client_from_settings(
            resolved_settings
        )

    report = await run_eval_suite(
        cases,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        bm25_index=index,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        rerank_provider=rerank,
        terms=resolved_terms,
        graph_client=resolved_graph_client,
    )

    print(f"评测用例数: {len(report.case_results)}")
    print(f"Context Recall 平均分: {_fmt(report.average_context_recall)}")
    print(f"Faithfulness 平均分: {_fmt(report.average_faithfulness)}")
    print(f"Answer Relevancy 平均分: {_fmt(report.average_answer_relevancy)}")
    print(f"专有名词准确率平均分: {_fmt(report.average_terminology_accuracy)}")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行评测集，输出 RAGAS 类指标汇总")
    parser.add_argument("--dataset", required=True, type=Path, help="评测集 JSONL 文件路径")
    parser.add_argument(
        "--use-graph",
        action="store_true",
        help="启用 TermGuard/术语表专有名词准确率评测（需先配置好术语表与 Neo4j）",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(dataset_path=args.dataset, use_graph=args.use_graph))
