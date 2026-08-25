from __future__ import annotations

from typing import Any

from app.agent.tool_registry import ToolContext
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorRecord


class VectorSearchTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        return raw_arguments

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> tuple[dict[str, Any], list[VectorRecord]]:
        """薄封装 hybrid_search，跟今天 app/agent/tools.py::vector_search_tool()
        逻辑一致——tenant_id 只能来自 context（由 tool_call_node 从
        AgentState 注入），不出现在 manifest.yaml 的 parameters_schema
        里，LLM 无法控制。原始 VectorRecord 列表随观察结果一起返回，供
        run_tool_calls 更新 retrieved_records/used_sources。"""
        query = str(arguments.get("query", ""))
        records = await hybrid_search(
            query,
            embedding_registry=context.embedding_registry,
            embedding_provider_name=context.embedding_provider_name,
            vector_store=context.vector_store,
            bm25_index=context.bm25_index,
            llm_registry=context.llm_registry,
            llm_provider_name=context.llm_provider_name,
            tenant_id=context.tenant_id,
            rerank_provider=context.rerank_provider,
            query_rewrite_enabled=context.query_rewrite_enabled,
            final_top_k=3,
        )
        observation = {"results": [{"id": r.id, "text": r.text} for r in records]}
        return observation, records


TOOL = VectorSearchTool()
