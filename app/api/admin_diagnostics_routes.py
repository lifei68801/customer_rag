from __future__ import annotations

import json
from typing import Any

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api import deps
from app.memory.qa_diagnostics import get_diagnostic, list_diagnostics

router = APIRouter(
    prefix="/api/admin/{tenant_id}/diagnostics",
    dependencies=[Depends(deps.require_admin_session)],
)


class DiagnosticSummary(BaseModel):
    id: int
    session_id: str
    question: str
    answer: str
    created_at: str


class DiagnosticListResponse(BaseModel):
    diagnostics: list[DiagnosticSummary]


class MentionedTerm(BaseModel):
    node_key: str
    standard_name: str | None = None
    term_type: str | None = None


class DiagnosticDetailResponse(BaseModel):
    id: int
    session_id: str
    question: str
    resolved_question: str | None
    answer: str
    used_sources: list[str]
    tool_results: list[dict[str, Any]]
    created_at: str
    #: 本次问答碰到的实体，去重后按出现顺序。空数组是个真实答案——说明这次
    #: 走的是纯向量检索，图谱一点没用上，本身就是重要线索。
    mentioned_terms: list[MentionedTerm]


def _walk(node: Any):
    """深度遍历任意嵌套的 JSON 结构，吐出每一个 dict。

    node_key 散落在 anchors / candidates / neighbors 三种结构里，而且还在
    不同深度上。按固定路径去取，每加一个工具就得补一条路径，漏掉的表现是
    「这个实体明明被用到了却没列出来」——排查的人会据此排除掉真正的元凶。
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def extract_mentioned_terms(tool_results: list[dict[str, Any]]) -> list[MentionedTerm]:
    """从工具结果里抽出所有被碰到的实体，去重保序。

    同一个实体在多轮工具调用里反复出现是常态，列三遍只会让人以为它被用了
    三次。保序是因为第一次出现的位置通常最接近问题的起点。
    """
    seen: set[str] = set()
    terms: list[MentionedTerm] = []
    for result in tool_results:
        try:
            observation = json.loads(str(result.get("content", "")))
        except (json.JSONDecodeError, TypeError):
            # content 不是合法 JSON（工具报错时会写别的东西）也要能打开
            # 诊断页——正是出错那次最需要看。
            continue
        for node in _walk(observation):
            node_key = node.get("node_key")
            if not isinstance(node_key, str) or node_key in seen:
                continue
            seen.add(node_key)
            terms.append(
                MentionedTerm(
                    node_key=node_key,
                    standard_name=node.get("standard_name"),
                    term_type=node.get("term_type") or node.get("type"),
                )
            )
    return terms


@router.get("", response_model=DiagnosticListResponse)
async def list_qa_diagnostics(
    tenant_id: str,
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> DiagnosticListResponse:
    rows = await list_diagnostics(
        memory_conn, tenant_id=tenant_id, session_id=session_id, limit=limit
    )
    return DiagnosticListResponse(diagnostics=[DiagnosticSummary(**row) for row in rows])


@router.get("/{diagnostic_id}", response_model=DiagnosticDetailResponse)
async def get_qa_diagnostic(
    tenant_id: str,
    diagnostic_id: int,
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
) -> DiagnosticDetailResponse:
    record = await get_diagnostic(
        memory_conn, tenant_id=tenant_id, diagnostic_id=diagnostic_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail=f"诊断记录不存在：{diagnostic_id}")
    return DiagnosticDetailResponse(
        **record, mentioned_terms=extract_mentioned_terms(record["tool_results"])
    )
