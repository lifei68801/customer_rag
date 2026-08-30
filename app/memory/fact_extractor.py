from __future__ import annotations

import json
import logging

from app.memory.llm_call import run_llm_text
from app.providers.base import ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是长期记忆事实抽取器。"
    "请从这一轮对话中抽取值得长期记住的事实（客户偏好、已确认的产品配置、"
    "长期约束），忽略寒暄和无意义内容。"
    '只输出 JSON：{"facts":["..."]}，抽不出就返回空列表。'
)


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
    return [str(item).strip() for item in raw if str(item).strip()]
