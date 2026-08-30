from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import build_agent_graph
from app.agent.tool_registry import discover_tools
from app.config.settings import Settings
from app.eval.dataset import EvalCase, load_eval_cases
from app.eval.llm_judged_metrics import score_answer_relevancy, score_faithfulness
from app.eval.metrics import score_context_recall
from app.eval.terminology_accuracy import score_terminology_accuracy
from app.graphrag.factory import build_graph_client_from_settings
from app.graphrag.neo4j_client import Neo4jGraphClient
from app.graphrag.ontology import Term
from app.graphrag.ontology_store import open_ontology_store_conn
from app.graphrag.term_guard import GraphClientProtocol
from app.graphrag.terms_store import list_terms
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


@dataclass(frozen=True)
class PlannerComparisonReport:
    planner_disabled: EvalReport
    planner_enabled: EvalReport


def _average(values: list[float | None]) -> float | None:
    """跳过 None（未评出分数的用例），只对真正评出分数的用例求平均。"""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


async def _score_case(
    case: EvalCase,
    *,
    answer_text: str,
    used_sources: list[str],
    retrieved_context: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term] | None,
) -> EvalCaseResult:
    """对已经产出答案的一个用例打分，供 run_eval_suite/run_eval_suite_via_agent_graph
    共用——两者获取答案的方式不同（直连 answer_question vs 走完整 Agent graph），
    但打分逻辑必须完全一致，否则两边的分数没有可比性。
    """
    context_recall = score_context_recall(case, used_sources)
    faithfulness = await score_faithfulness(
        answer=answer_text,
        context=retrieved_context,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
    )
    answer_relevancy = await score_answer_relevancy(
        question=case.question,
        answer=answer_text,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
    )
    terminology_accuracy = (
        score_terminology_accuracy(answer_text, terms) if terms else None
    )
    return EvalCaseResult(
        question=case.question,
        answer=answer_text,
        context_recall=context_recall,
        faithfulness=faithfulness,
        answer_relevancy=answer_relevancy,
        terminology_accuracy=terminology_accuracy,
    )


def _build_report(case_results: list[EvalCaseResult]) -> EvalReport:
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


async def run_eval_suite(
    cases: list[EvalCase],
    *,
    embedding_registry: EmbeddingRegistry,
    embedding_provider_name: str,
    vector_store: VectorStore,
    bm25_index: BM25Index,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    tenant_id: str,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: GraphClientProtocol | None = None,
    top_k: int = 3,
) -> EvalReport:
    """对评测集里的每个用例真正跑一遍检索+生成，汇总 RAGAS 类指标。

    直接调用 answer_question()——只跑"检索+生成"这一段，不经过 Agent graph
    的 InputSafety/TermGuard/Memory/OutputSafety 等节点。想对比走完整 Agent
    流程（含 Planner）的效果，用 run_eval_suite_via_agent_graph()。

    整个评测集只针对一个 tenant_id 跑——评测的是"这个租户的知识库
    回答得好不好"，不存在跨租户混跑的场景。

    Faithfulness/Answer Relevancy 是 LLM 裁判打分，会真实调用一次 LLM
    （非 answer_question 生成阶段那次），因此跑一个完整评测集的耗时和
    成本是 O(4N) 次 LLM 调用（N=用例数：生成 1 次 + answer_question 内部
    output safety 的 semantic_safety_review 1 次 + 这里的两次裁判打分各
    1 次），比只做确定性指标贵得多。
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
            tenant_id=tenant_id,
            rerank_provider=rerank_provider,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=terms,
            graph_client=graph_client,
            top_k=top_k,
        )
        case_results.append(
            await _score_case(
                case,
                answer_text=result.text,
                used_sources=result.used_sources,
                retrieved_context=result.retrieved_context,
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                terms=terms,
            )
        )

    return _build_report(case_results)


async def run_eval_suite_via_agent_graph(
    cases: list[EvalCase],
    *,
    graph: CompiledStateGraph,
    tenant_id: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    terms: list[Term] | None = None,
) -> EvalReport:
    """跟 run_eval_suite 完全一样的打分口径，但答案来自完整 Agent graph
    （InputSafety/TermGuard/MemoryRecall/确定性检索或 Planner/OutputSafety
    全走一遍），不是直接调 answer_question()。

    graph 由调用方预先通过 build_agent_graph() 构建好再传入——是否开启
    Planner（enable_autonomous_planning）、要不要接记忆等由 graph 自身的
    构建参数决定；这个函数只管"喂问题进去、把结果接进同一套打分流水线"。
    这正是用来对比 Planner 开/关两种模式效果的入口：分别用
    enable_autonomous_planning=False/True 构建两个 graph，各跑一次这个
    函数，比较 average_* 指标。

    每个用例用独立的 session_id（"eval-{index}"），避免评测跑多条用例时
    互相污染同一个会话的记忆滑窗。
    """
    case_results: list[EvalCaseResult] = []
    for index, case in enumerate(cases):
        result = await graph.ainvoke(
            {
                "question": case.question,
                "tenant_id": tenant_id,
                "session_id": f"eval-{index}",
                "user_id": "eval",
            }
        )
        answer_text = result.get("final_text", "")
        used_sources = result.get("used_sources", [])
        retrieved_context = "\n\n".join(
            record.text for record in result.get("retrieved_records", [])
        )
        case_results.append(
            await _score_case(
                case,
                answer_text=answer_text,
                used_sources=used_sources,
                retrieved_context=retrieved_context,
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                terms=terms,
            )
        )

    return _build_report(case_results)


async def compare_planner_modes(
    cases: list[EvalCase],
    *,
    tenant_id: str,
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
    max_tool_call_rounds: int = 3,
) -> PlannerComparisonReport:
    """分别用 enable_autonomous_planning=False/True 构建两个 Agent graph，
    各跑一遍同一份评测集，用完全相同的打分口径对比两种模式的效果。

    这是 docs/AGENT_PLANNER_DESIGN.md 第8步"用真实评测数据决定是否把
    AgentSettings.enable_autonomous_planning 默认值翻转为 True"的落地
    入口——两个 graph 除了这一个开关，其余构建参数完全一致，保证对比
    的唯一变量就是 Planner 开关本身。

    build_agent_graph 的 tool_registry 现在是必填参数（见
    app/agent/graph.py 的说明），这里用 discover_tools 扫描
    app/agent/tools/ 目录构建一次，两个 graph 共用同一份注册表——跟
    app/api/deps.py::get_tool_registry 的单例接入方式是同一套发现逻辑，
    只是评测脚本没有跨请求复用的必要，直接每次调用扫描一次即可。
    """
    tool_registry = discover_tools(Path(__file__).resolve().parent.parent / "agent" / "tools")
    common_kwargs = dict(
        embedding_registry=embedding_registry,
        embedding_provider_name=embedding_provider_name,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        tool_registry=tool_registry,
        rerank_provider=rerank_provider,
        query_rewrite_enabled=query_rewrite_enabled,
        terms=terms,
        graph_client=graph_client,
        top_k=top_k,
    )

    planner_disabled_graph = build_agent_graph(
        **common_kwargs, enable_autonomous_planning=False
    )
    planner_enabled_graph = build_agent_graph(
        **common_kwargs,
        enable_autonomous_planning=True,
        max_tool_call_rounds=max_tool_call_rounds,
    )

    planner_disabled_report = await run_eval_suite_via_agent_graph(
        cases,
        graph=planner_disabled_graph,
        tenant_id=tenant_id,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        terms=terms,
    )
    planner_enabled_report = await run_eval_suite_via_agent_graph(
        cases,
        graph=planner_enabled_graph,
        tenant_id=tenant_id,
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        terms=terms,
    )

    return PlannerComparisonReport(
        planner_disabled=planner_disabled_report,
        planner_enabled=planner_enabled_report,
    )


def _fmt(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "N/A（无有效评分）"


def _print_report(report: EvalReport, *, label: str | None = None) -> None:
    if label:
        print(f"--- {label} ---")
    print(f"评测用例数: {len(report.case_results)}")
    print(f"Context Recall 平均分: {_fmt(report.average_context_recall)}")
    print(f"Faithfulness 平均分: {_fmt(report.average_faithfulness)}")
    print(f"Answer Relevancy 平均分: {_fmt(report.average_answer_relevancy)}")
    print(f"专有名词准确率平均分: {_fmt(report.average_terminology_accuracy)}")


async def main(
    *,
    dataset_path: Path,
    tenant_id: str,
    settings: Settings | None = None,
    embedding_registry: EmbeddingRegistry | None = None,
    vector_store: VectorStore | None = None,
    bm25_index: BM25Index | None = None,
    llm_registry: ProviderRegistry | None = None,
    rerank_provider: RerankProvider | None = None,
    query_rewrite_enabled: bool = True,
    terms: list[Term] | None = None,
    graph_client: Neo4jGraphClient | None = None,
    use_graph: bool = False,
    compare_planner: bool = False,
    max_tool_call_rounds: int = 3,
) -> EvalReport | PlannerComparisonReport:
    """评测集运行脚本入口：加载 JSONL 评测集，跑一遍检索+生成，输出 RAGAS 类指标汇总。

    用法：
      python -m app.eval.runner --dataset app/eval/eval_seed.jsonl --tenant-id t1
      python -m app.eval.runner --dataset app/eval/eval_seed.jsonl --tenant-id t1 --use-graph
      python -m app.eval.runner --dataset app/eval/eval_seed.jsonl --tenant-id t1 --compare-planner

    --compare-planner：不跑 answer_question() 直连路径，改为分别用
    enable_autonomous_planning=False/True 构建两个 Agent graph 各跑一遍，
    输出两组指标供对比——这是决定要不要把
    AgentSettings.enable_autonomous_planning 默认值翻转为 True 的数据来源。
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
        resolved_review_conn = await open_ontology_store_conn(resolved_settings)
        try:
            resolved_terms = terms or await list_terms(resolved_review_conn, tenant_id)
        finally:
            await resolved_review_conn.close()
        if graph_client is not None:
            resolved_graph_client = graph_client
        else:
            resolved_graph_client = build_graph_client_from_settings(resolved_settings)
            await resolved_graph_client.ensure_tenant_scoped_schema()

    if compare_planner:
        comparison = await compare_planner_modes(
            cases,
            tenant_id=tenant_id,
            embedding_registry=registry,
            embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
            vector_store=store,
            bm25_index=index,
            llm_registry=llm,
            llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
            rerank_provider=rerank,
            query_rewrite_enabled=query_rewrite_enabled,
            terms=resolved_terms,
            graph_client=resolved_graph_client,
            max_tool_call_rounds=max_tool_call_rounds,
        )
        _print_report(comparison.planner_disabled, label="Planner 关闭（确定性路径）")
        _print_report(comparison.planner_enabled, label="Planner 开启（自主规划路径）")
        return comparison

    report = await run_eval_suite(
        cases,
        embedding_registry=registry,
        embedding_provider_name=DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=store,
        bm25_index=index,
        llm_registry=llm,
        llm_provider_name=DEFAULT_LLM_PROVIDER_NAME,
        tenant_id=tenant_id,
        rerank_provider=rerank,
        query_rewrite_enabled=query_rewrite_enabled,
        terms=resolved_terms,
        graph_client=resolved_graph_client,
    )
    _print_report(report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行评测集，输出 RAGAS 类指标汇总")
    parser.add_argument("--dataset", required=True, type=Path, help="评测集 JSONL 文件路径")
    parser.add_argument("--tenant-id", required=True, help="本次评测针对的租户/产品线标识")
    parser.add_argument(
        "--use-graph",
        action="store_true",
        help="启用 TermGuard/术语表专有名词准确率评测（需先配置好术语表与 Neo4j）",
    )
    parser.add_argument(
        "--compare-planner",
        action="store_true",
        help=(
            "对比 Agent 自主规划开/关两种模式的评测指标（分别构建两个 Agent graph "
            "跑同一份评测集），而不是走 answer_question() 直连路径"
        ),
    )
    parser.add_argument(
        "--max-tool-call-rounds",
        type=int,
        default=3,
        help="--compare-planner 时 Planner 开启一侧的最大工具调用轮次上限",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            dataset_path=args.dataset,
            tenant_id=args.tenant_id,
            use_graph=args.use_graph,
            compare_planner=args.compare_planner,
            max_tool_call_rounds=args.max_tool_call_rounds,
        )
    )
