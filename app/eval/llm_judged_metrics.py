from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_FAITHFULNESS_SYSTEM_PROMPT = (
    "你是 RAG 回答质量评审员。判断【回答】的内容是否完全基于【资料】，"
    "没有编造资料之外的信息。给出 0.0-1.0 的分数，1.0 表示完全基于资料，"
    "0.0 表示大量编造内容。"
    '只输出 JSON：{"score": 0.0}'
)

_ANSWER_RELEVANCY_SYSTEM_PROMPT = (
    "你是 RAG 回答质量评审员。判断【回答】是否切实回应了【问题】。"
    "给出 0.0-1.0 的分数，1.0 表示完全切题，0.0 表示答非所问。"
    '只输出 JSON：{"score": 0.0}'
)


async def _score_via_llm(
    *,
    system_prompt: str,
    user_content: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float,
) -> float | None:
    """返回 LLM 给出的 0-1 分数；失败/超时/解析失败均返回 None（不是 0.0）。

    None 表示"这条评测未评出分数"，聚合统计时应该被排除，而不是当作
    "评了 0 分"计入平均值——否则 LLM 裁判本身的不稳定性会被误当成
    "回答质量真的很差"，污染评测报告的可信度。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("LLM 评测裁判超时，本条不计分")
        return None
    except Exception:
        logger.warning("LLM 评测裁判失败，本条不计分", exc_info=True)
        return None

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "score" not in payload:
        return None
    try:
        score = float(payload["score"])
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))


async def score_faithfulness(
    *,
    answer: str,
    context: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> float | None:
    return await _score_via_llm(
        system_prompt=_FAITHFULNESS_SYSTEM_PROMPT,
        user_content=f"【资料】\n{context}\n\n【回答】\n{answer}",
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=timeout_sec,
    )


async def score_answer_relevancy(
    *,
    question: str,
    answer: str,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> float | None:
    return await _score_via_llm(
        system_prompt=_ANSWER_RELEVANCY_SYSTEM_PROMPT,
        user_content=f"【问题】\n{question}\n\n【回答】\n{answer}",
        llm_registry=llm_registry,
        llm_provider_name=llm_provider_name,
        timeout_sec=timeout_sec,
    )
