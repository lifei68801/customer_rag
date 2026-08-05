from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是记忆冲突决策器。根据新事实和历史记忆，为每条新事实决定动作。"
    "动作仅允许 ADD/UPDATE/DELETE/NONE："
    "ADD=历史不存在该信息；UPDATE=同主题但内容更新（需给出 target_memory_id）；"
    "DELETE=新事实明确否定旧事实（需给出 target_memory_id）；"
    "NONE=重复或无价值。"
    '只输出 JSON：{"actions":[{"event":"...","target_memory_id":"","text":"...","reason":"..."}]}'
)


async def resolve_memory_actions(
    *,
    new_facts: list[str],
    existing_memories: list[dict[str, Any]],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> list[dict[str, str]]:
    """LLM 判断每条新事实的记忆动作；失败/超时/JSON 解析失败降级为规则兜底。

    规则兜底：新事实的文本若与已有记忆文本完全一致则判 NONE，否则判 ADD——
    不做 UPDATE/DELETE 的规则判断，这类语义变化的判断没有可靠的确定性
    规则替代，宁可保守新增也不要在规则层猜测更新/删除哪一条。
    """
    if not new_facts:
        return []
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "new_facts": new_facts,
                                    "existing_memories": existing_memories,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("记忆冲突决策超时，降级规则模式")
        return _fallback_actions(new_facts, existing_memories)
    except Exception:
        logger.warning("记忆冲突决策失败，降级规则模式", exc_info=True)
        return _fallback_actions(new_facts, existing_memories)

    actions = _parse_actions(result.text)
    return actions if actions else _fallback_actions(new_facts, existing_memories)


def _parse_actions(text: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    raw = payload.get("actions") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    actions: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event", "")).upper().strip()
        if event not in {"ADD", "UPDATE", "DELETE", "NONE"}:
            continue
        actions.append(
            {
                "event": event,
                "memory_id": str(item.get("target_memory_id", "")).strip(),
                "text": str(item.get("text", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    return actions


def _fallback_actions(
    new_facts: list[str], existing_memories: list[dict[str, Any]]
) -> list[dict[str, str]]:
    existing_texts = {str(item.get("text", "")).strip() for item in existing_memories}
    actions: list[dict[str, str]] = []
    for fact in new_facts:
        text = fact.strip()
        if not text:
            continue
        event = "NONE" if text in existing_texts else "ADD"
        actions.append(
            {"event": event, "memory_id": "", "text": text, "reason": "fallback"}
        )
    return actions
