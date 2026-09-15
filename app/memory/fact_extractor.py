from __future__ import annotations

import json
import logging
import re

from app.memory.llm_call import run_llm_text
from app.providers.base import ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是长期记忆事实抽取器。"
    "请从这一轮对话中抽取值得长期记住的事实（客户偏好、已确认的产品配置、"
    "长期约束），忽略寒暄和无意义内容。"
    "**只抽取用户陈述或用户确认过的内容。助手从知识库/图谱查出来的结果"
    "（各种计数、统计、实体清单）不是事实，不要抽取**——那些数据活在知识库里，"
    "会随数据更新而变，抄进长期记忆就成了一份永不更新的快照，还会在后续对话里"
    "盖过实时查询的结果。"
    '只输出 JSON：{"facts":["..."]}，抽不出就返回空列表。'
)

#: 半角/全角数字和常见千分位写法。守卫按"这个数字在哪一侧出现过"判断来源。
_NUMBER_PATTERN = re.compile(r"\d[\d,，.]*")


def _normalize_number(raw: str) -> str:
    """把 "10,000"/"10，000"/"10000." 归一成 "10000"，好跨措辞比对。"""
    return raw.replace(",", "").replace("，", "").rstrip(".")


def drop_facts_whose_numbers_came_only_from_the_assistant(
    facts: list[str], *, user_input: str, assistant_output: str
) -> list[str]:
    """丢掉那些"数字只来自助手回答"的事实。

    这是提示词之外的一道确定性守卫，针对真实发生过的一类污染：demo 租户的
    76 条长期记忆里有 37 条是知识库查询结果，其中 15 条是同一句"Coca-Cola
    公司共有 10,000 个订单"的不同措辞。那个 10000 是当时图谱的一个中间结果，
    数据改一次就错了——而它会在上下文里盖过工具的实时返回（实测：工具返回
    matched_count=0，模型仍然答出"匹配到 10000 条"）。

    判据是**来源**而不是措辞：事实里出现的数字，如果在助手回答里有、在用户
    那句话里没有，说明这个数来自知识库查询，不是用户告诉系统的。用户自己说
    的数（"我要 20 个席位""发票抬头写 91310000..."）照常保留。

    已知代价：助手陈述、用户只回一句"对"的那类确认（"您的套餐是 50 人版" →
    "对"），里面的数字也会被丢掉。这是有意的取舍——那类确认本来就该由用户
    复述一遍才进长期记忆，而把知识库计数当成事实的代价要大得多。
    """
    user_numbers = {_normalize_number(m) for m in _NUMBER_PATTERN.findall(user_input)}
    assistant_numbers = {_normalize_number(m) for m in _NUMBER_PATTERN.findall(assistant_output)}
    kept: list[str] = []
    for fact in facts:
        numbers = {_normalize_number(m) for m in _NUMBER_PATTERN.findall(fact)}
        derived = {n for n in numbers if n in assistant_numbers and n not in user_numbers}
        if derived:
            logger.info(
                "丢弃一条数字只来自助手回答的事实（疑似知识库查询结果）：%r，数字 %s",
                fact[:80], sorted(derived),
            )
            continue
        kept.append(fact)
    return kept


async def extract_facts(
    *,
    user_input: str,
    assistant_output: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> list[str]:
    """从一轮对话抽取长期记忆事实；失败/超时/JSON 解析失败均回退空列表。"""
    response_text = await run_llm_text(
        llm_registry=llm_registry,
        request=ProviderRequest(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"用户：{user_input}\n助手：{assistant_output}",
                },
            ]
        ),
        provider_name=llm_provider_name,
        timeout_sec=timeout_sec,
        label="事实抽取",
        fallback_label="回退空列表",
    )
    if response_text is None:
        return []

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        logger.warning("事实抽取返回非 JSON，回退空列表")
        return []

    raw = payload.get("facts") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    facts = [str(item).strip() for item in raw if str(item).strip()]
    return drop_facts_whose_numbers_came_only_from_the_assistant(
        facts, user_input=user_input, assistant_output=assistant_output
    )
