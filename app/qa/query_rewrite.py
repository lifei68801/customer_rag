from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是客服问答检索的 query 改写助手。"
    "如果用户的问题已经清晰、具体，直接原样返回，不要改写。"
    "只有当问题用词过于口语化、不利于文档检索匹配时，才改写成更规范的术语表达。"
    "改写后的句子必须保留原始问题里所有具体词语和限定条件，"
    "不能为了检索友好而丢弃或概括它们。"
    "只输出改写后的一句话，不要解释。"
)


async def rewrite_query(
    question: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 1.0,
    conversation_context: list[dict[str, str]] | None = None,
) -> str:
    """尝试用 LLM 改写检索 query；失败/超时/空结果均回退原始 question，不阻塞主链路。

    conversation_context 为可选项，保留是为了向后兼容既有调用方
    （app/qa/answer.py 的确定性路径）。Planner/Agent 路径不再需要传它：
    跨轮次指代消解已经统一由 Layer 1（resolve_question）在更上游解决一次，
    这里不再重复承担这个职责，只负责把口语化表达改写得更利于文档检索匹配。
    """
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    if conversation_context:
        messages.extend(conversation_context)
    messages.append({"role": "user", "content": question})

    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(messages=messages),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("query 改写超时，回退原始 query")
        return question
    except Exception:
        logger.warning("query 改写失败，回退原始 query", exc_info=True)
        return question

    rewritten = result.text.strip()
    return rewritten or question


_SLOT_NAMES = ("anchor", "intent_type", "constraint")

_RESOLVE_QUESTION_SYSTEM_PROMPT = (
    "你是多轮对话的指代消解助手。给定最近几轮对话历史和用户当前这一句话，"
    "判断当前这句话脱离历史后是否仍能独立理解、执行。\n\n"
    "把问题拆成三类槽位：\n"
    "- anchor：问的是哪个具体实体或实体类型\n"
    "- intent_type：问题的意图类型（计数/列举/查详情/比较）\n"
    "- constraint：过滤/限定条件（属于哪个公司、大于多少等）\n\n"
    "默认不改写：只有当前问题里某个槽位明显缺失、必须借助历史才能补全"
    "（比如用指代词「它/这个/上面提到的」代替了 anchor，或者只提到"
    "constraint 却没交代 intent_type），才判定为依赖历史。当前问题里已经"
    "显式出现的槽位（尤其是「多少个/数量/一共/共有」这类 intent_type=计数"
    "的措辞）必须原样保留，禁止被历史覆盖或省略。\n\n"
    '只输出 JSON：{"rl": 1或3, "resolved_question": "...", '
    '"inherited_slots": [...], "duplicate_of": "..."}\n'
    "rl=3（默认）：不依赖历史，resolved_question 必须逐字等于用户当前问题，"
    "inherited_slots 为空数组。\n"
    "rl=1：依赖历史，resolved_question 只补全缺失槽位对应的内容，不改写"
    "当前问题里已经出现的其余内容；inherited_slots 精确列出这次实际从"
    "历史补全了哪些槽位（anchor/intent_type/constraint 的子集，只填真正"
    "补全的，当前问题里本来就有的槽位不算继承）。\n"
    "duplicate_of：如果 resolved_question 在语义上跟历史里某一轮用户已经"
    "问过、且已经得到回答的问题基本相同，把那一轮的原始用户提问文本填在"
    "这里；没有这种情况就填空字符串。"
)


@dataclass(frozen=True)
class ResolvedQuestion:
    """Layer 1（历史指代消解）的产出。

    resolved_question：消解指代后、可以独立执行的问题；不依赖历史时逐字
    等于用户原问题。
    inherited_slots：这次实际从历史补全了哪些槽位，取值只可能是
    anchor/intent_type/constraint。目前没有下游消费，只落日志，用于复测时
    排查"槽位填充到底有没有生效、生效在哪个槽位"。
    duplicate_of：命中的历史轮次原文（当前问题跟它基本是同一个问题）；
    没命中是 None。供设计 D 的重复提问软提示使用。
    """

    resolved_question: str
    inherited_slots: list[str]
    duplicate_of: str | None


async def resolve_question(
    question: str,
    history: list[dict[str, str]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 1.5,
) -> ResolvedQuestion:
    """历史指代消解（槽位粒度），顺带检测当前问题是否在问一个最近已经问过
    并回答过的问题（供重复提问软提示使用，同一次 LLM 调用产出，不新增调用）。

    这是"改写"两层架构的 Layer 1：只解决跨轮次指代，一次性执行、结果在这
    一轮内保持稳定。Layer 2（Planner 每轮根据工具反馈调整 query_intent）是
    另一件事，不在这里处理。

    失败/超时/解析失败一律回退"原样返回问题、无槽位继承、无重复"——这是这
    个函数"下限不比不做这一步差"的保证，跟 rewrite_query() 的失败处理原则
    一致：这一步是增强，不是必经关卡，不能因为它抖动就让整轮对话失败。

    注意这里不对模型的输出做任何确定性校验（比如检查改写后有没有丢失计数
    关键词）——设计上明确决定完全依赖提示词，见设计文档
    docs/superpowers/specs/2026-08-27-query-matching-and-rewrite-redesign-design.md
    的"2026-08-28 决策变更"一节。rl 字段同理只起"强制模型做一次显式决策"
    的作用，不参与任何分支逻辑。
    """
    messages = [
        {"role": "system", "content": _RESOLVE_QUESTION_SYSTEM_PROMPT},
        {"role": "user", "content": f"history: {history}\nquestion: {question}"},
    ]
    fallback = ResolvedQuestion(
        resolved_question=question, inherited_slots=[], duplicate_of=None
    )
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(messages=messages),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("resolve_question 超时，回退原始问题")
        return fallback
    except Exception:
        logger.warning("resolve_question 调用失败，回退原始问题", exc_info=True)
        return fallback

    try:
        payload = json.loads(result.text)
    except (json.JSONDecodeError, TypeError):
        # TypeError 也要接住：provider 违反 ProviderResult.text 的 str 类型
        # 契约、给出 None 时，json.loads(None) 抛的是 TypeError 而不是
        # JSONDecodeError。这个函数没有外层 try/except（调用方
        # graph.py::resolve_question_node 直接 await 它），漏接会把整轮对话
        # 打挂，而不只是这一步消解失败——违背"失败就退回原问题、绝不影响
        # 主链路"这条设计前提。
        logger.warning("resolve_question 返回内容不是合法 JSON，回退原始问题")
        return fallback
    if not isinstance(payload, dict):
        logger.warning("resolve_question 返回的 JSON 不是对象，回退原始问题")
        return fallback

    resolved = str(payload.get("resolved_question") or "").strip() or question
    raw_slots = payload.get("inherited_slots") or []
    inherited = [s for s in raw_slots if s in _SLOT_NAMES] if isinstance(raw_slots, list) else []
    duplicate_of = str(payload.get("duplicate_of") or "").strip() or None
    return ResolvedQuestion(
        resolved_question=resolved,
        inherited_slots=inherited,
        duplicate_of=duplicate_of,
    )
