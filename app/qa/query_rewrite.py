from __future__ import annotations

import asyncio
import logging

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
