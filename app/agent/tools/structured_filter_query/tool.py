from __future__ import annotations

import json
from typing import Any

from app.agent.tool_registry import ToolContext
from app.graphrag.ontology_recall import format_recall_candidates, recall_ontology_candidates
from app.graphrag.structured_filter_query import run_structured_filter_query
from app.providers.base import ProviderCapability, ProviderRequest
from app.retrieval.vector_store import VectorRecord

_USAGE_GUIDE = (
    "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
    "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
    "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
    "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
    "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
    "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
    "看到「多少个/数量」等计数意图时，必须以 anchor.term_type + constraints 模式返回的 "
    "matched_count 为准给出确定数字（anchor.name 模式的 matched_count 只表示"
    "「是否找到了这个实体」，是 0 或 1，不是数量答案）——不能仅凭检索到的文档片段或邻居关系"
    "列表猜测。constraints 里 standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
    "不要求填精确的标准名称。\n"
    "「xx类目/公司下有多少个yy」这类需要先确定xx是什么、再数yy数量的问题，"
    "优先用 anchor.term_type 定位 yy 的类型，配合 constraints.kind=relation"
    "（target_field=standard_name，target_value 直接填 xx 的口语化/别名名称，"
    "会自动模糊解析成标准名，不需要先把 xx 解析成具体实体再回填）——这是解决这类问题"
    "最直接的写法。只有当查询意图本身是在问「xx是什么/xx关联着什么」这类需要先明确xx"
    "具体所指的问题（而不是要数yy的数量）时，才用 anchor.name。\n"
    "如果 yy 和 xx 之间在候选参考的「可能相关的关系」里找不到直接的一跳关系，"
    "先检查候选参考里有没有「可能相关的多跳路径」——如果有，说明 yy 和 xx 之间"
    "隔着一个中间实体类型，必须把那条路径的每一跳（含各自的 relation_type/"
    "direction/target_term_type）原样抄进 constraints.hops，不能只抄第一跳就"
    "省略中间类型直接把 target_field/target_value 接到 yy 自己身上——那样等于"
    "没有对 xx 做任何过滤，返回的会是 yy 这个类型下的全部数量，不是 xx 名下的数量。"
)

_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor": {
            "type": "object",
            "description": "起点定位方式，二选一",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "已知的实体名称或别名"},
                        "type_hint": {
                            "type": "string",
                            "description": "该实体的类型（可选，同名实体存在多个类型时用于消歧）",
                        },
                    },
                    "required": ["name"],
                },
                {
                    "type": "object",
                    "properties": {
                        "term_type": {
                            "type": "string",
                            "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                        },
                    },
                    "required": ["term_type"],
                },
            ],
        },
        "constraints": {
            "type": "array",
            "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，"
                           "直接用解析出的锚点）。standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
                           "不要求填精确的标准名称——比如用户说的口语化名字可以直接填进来，系统会自动解析。",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["attribute", "relation"],
                        "description": "attribute：直接比较锚点实体自己的字段；relation：经过关系跳到目标实体再比较",
                    },
                    "field": {
                        "type": "string",
                        "description": "kind=attribute 时必填：要比较的字段名（standard_name 或该实体类型已声明的属性字段名）",
                    },
                    "operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "比较运算符，实际可用范围取决于字段类型",
                    },
                    "value": {"description": "kind=attribute 时必填：比较的目标值"},
                    "hops": {
                        "type": "array",
                        "description": "kind=relation 时必填：从锚点出发的关系跳数组，最多2跳",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relation_type": {"type": "string", "description": "关系类型，如 HAS_VARIANT"},
                                "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                                "target_term_type": {"type": "string", "description": "这一跳到达的实体类型"},
                            },
                            "required": ["relation_type", "direction", "target_term_type"],
                        },
                    },
                    "target_field": {
                        "type": "string",
                        "description": "kind=relation 时必填：在最后一跳到达的实体上比较哪个字段",
                    },
                    "target_operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "kind=relation 时必填：对 target_field 用的运算符",
                    },
                    "target_value": {"description": "kind=relation 时必填：比较的目标值"},
                },
                "required": ["kind"],
            },
        },
        "expand": {
            "type": ["object", "null"],
            "description": "可选：展开命中锚点的邻居关系",
            "properties": {
                "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                "relation_type": {
                    "type": ["string", "null"],
                    "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                },
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                    "description": "关系方向，默认 both",
                },
            },
        },
        "group_by": {
            "type": ["object", "null"],
            "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身",
            "properties": {
                "constraint_index": {
                    "type": "integer",
                    "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                },
            },
        },
        "limit": {
            "type": "integer",
            "description": "返回结果的最大条数，默认20——预期命中数量较多时"
                           "（如宽泛的数值区间过滤），请设置一个合理的值避免返回过多结果",
        },
    },
    "required": ["anchor"],
}


class ToolArgumentResolutionError(Exception):
    """resolve_arguments 失败时抛出——调用方（app/agent/planner.py 的
    run_tool_calls）捕获后降级成这次工具调用的 {"error": ...} 观察结果，
    不会让整个 Planner 轮次崩溃。"""


def _strip_json_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    return stripped


def _build_prompt(query_intent: str, original_question: str, candidates) -> str:
    schema_text = json.dumps(_PARAMETERS_SCHEMA, ensure_ascii=False, indent=2)
    return (
        "你是一个把自然语言查询意图转成结构化查询参数的助手。给定下面的查询意图、"
        "使用说明、JSON Schema、以及召回到的本体候选参考，输出一段严格匹配这个 "
        "JSON Schema 的 JSON 对象作为你的完整回复——不要输出任何 JSON 之外的文字，"
        "也不要用 markdown 代码块包裹。\n\n"
        f"使用说明：\n{_USAGE_GUIDE}\n\n"
        f"JSON Schema：\n{schema_text}\n\n"
        "constraints.hops 里的 relation_type/target_term_type、constraints 里的 "
        "field/target_field，以及 anchor.term_type，都应该优先使用下面候选参考里"
        "出现过的名字，不要凭空发明没见过的名字。\n\n"
        f"候选参考：\n{format_recall_candidates(candidates)}\n\n"
        f"查询意图（由上一步的助手改写整理）：{query_intent}\n\n"
        f"用户原始问题（未经改写，逐字原文）：{original_question}\n\n"
        "上一步的改写可能会丢失用户原话里「多少个/数量/一共有多少/有几个」这类"
        "计数意图——判断是否要用 anchor.term_type + constraints 模式（计数场景）"
        "时，必须同时参考用户原始问题；只要原始问题里出现了计数措辞，即使查询"
        "意图这句话里没有，也要按计数场景处理，不能因为改写丢词就退化成 "
        "anchor.name 模式。"
    )


class StructuredFilterQueryTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        query_intent = str(raw_arguments.get("query_intent") or "").strip() or context.question
        candidates = recall_ontology_candidates(
            f"{query_intent}\n{context.question}", terms=context.terms,
            term_type_schema=context.term_type_schema,
            allowed_combinations=context.allowed_combinations,
        )
        prompt = _build_prompt(query_intent, context.question, candidates)
        try:
            result = await context.llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(messages=[{"role": "user", "content": prompt}]),
                provider_name=context.llm_provider_name,
            )
        except Exception as exc:
            raise ToolArgumentResolutionError(f"参数生成调用失败：{exc}") from exc
        try:
            return json.loads(_strip_json_code_fence(result.text))
        except json.JSONDecodeError as exc:
            raise ToolArgumentResolutionError(
                f"参数生成调用返回的内容不是合法 JSON：{result.text[:200]!r}"
            ) from exc

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> tuple[dict[str, Any], list[VectorRecord]]:
        if context.graph_client is None:
            return {"error": "structured_filter_query_tool 未配置"}, []
        observation = await run_structured_filter_query(
            arguments, terms=context.terms, graph_client=context.graph_client,
            tenant_id=context.tenant_id,
            confirmed_relation_types=context.confirmed_relation_types,
            term_type_schema=context.term_type_schema,
        )
        return observation, []


TOOL = StructuredFilterQueryTool()
