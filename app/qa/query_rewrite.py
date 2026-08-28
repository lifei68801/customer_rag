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
    "请把用户的口语化提问改写为更利于文档检索的表达："
    "结合此前的对话历史（如果提供）补全模糊指代（比如“这个报错”指代的具体错误码/模块），"
    "尽量使用规范术语。"
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

    conversation_context 为可选项：传入近期对话轮次（如
    app/memory/context_injection.py 组装的 memory_context_messages）时，
    改写 LLM 能看到"用户之前说了什么"来补全"这个报错""刚才那个"之类的
    模糊指代；不传则只看孤立的当前问题，行为与之前完全一致。
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
    except json.JSONDecodeError:
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
