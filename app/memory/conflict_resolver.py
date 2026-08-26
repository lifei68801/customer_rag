from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_VALID_CONFLICT_TYPES = {"value", "temporal", "logical"}

_SYSTEM_PROMPT = (
    "你是记忆冲突决策器。根据新事实和历史记忆，为每条新事实决定动作和冲突类型。"
    "动作仅允许 ADD/UPDATE/DELETE/NONE："
    "ADD=历史不存在该信息；UPDATE=同主题但内容更新（需给出 target_memory_id）；"
    "DELETE=新事实明确否定旧事实（需给出 target_memory_id，reason 必须引用新事实"
    "的具体内容作为依据，不能是空泛的理由）；NONE=重复或无价值。"
    "冲突类型（conflict_type）仅在 UPDATE/DELETE 时给出，三选一："
    "value=同一属性的值发生变化（如住址、联系方式偏好）；"
    "temporal=不同时间点的陈述不一致，需要判断谁更新；"
    "logical=语义上互斥、需要推理才能发现的矛盾。"
    "ADD/NONE 不需要 conflict_type。"
    '只输出 JSON：{"actions":[{"event":"...","target_memory_id":"","text":"...",'
    '"reason":"...","conflict_type":"..."}]}'
)


async def resolve_memory_actions(
    *,
    new_facts: list[str],
    existing_memories: list[dict[str, Any]],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> list[dict[str, str]]:
    """LLM 判断每条新事实的记忆动作+冲突类型；失败/超时/JSON 解析失败降级为
    规则兜底。发起 LLM 调用之前先做一道精确文本去重短路——跟已有记忆文本
    完全一致的新事实直接判 NONE，不占用一次 LLM 调用；这一步只做字符串
    相等判断，不做相似度计算（相似度层面的判断留给 LLM 的 conflict_type
    分类）。

    规则兜底（LLM 调用失败/超时/解析失败时）：新事实的文本若与已有记忆文本
    完全一致则判 NONE，否则判 ADD——不做 UPDATE/DELETE 的规则判断，这类
    语义变化的判断没有可靠的确定性规则替代，宁可保守新增也不要在规则层
    猜测更新/删除哪一条。
    """
    if not new_facts:
        return []

    existing_texts = {str(item.get("text", "")).strip() for item in existing_memories}
    short_circuit_actions: list[dict[str, str]] = []
    llm_facts: list[str] = []
    for fact in new_facts:
        text = fact.strip()
        if text in existing_texts:
            short_circuit_actions.append(
                {
                    "event": "NONE", "memory_id": "", "text": text,
                    "reason": "精确文本重复", "conflict_type": "",
                }
            )
        else:
            llm_facts.append(fact)

    if not llm_facts:
        return short_circuit_actions

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
                                    "new_facts": llm_facts,
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
        return short_circuit_actions + _fallback_actions(llm_facts, existing_memories)
    except Exception:
        logger.warning("记忆冲突决策失败，降级规则模式", exc_info=True)
        return short_circuit_actions + _fallback_actions(llm_facts, existing_memories)

    actions = _parse_actions(result.text)
    if not actions:
        actions = _fallback_actions(llm_facts, existing_memories)
    return short_circuit_actions + actions


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
        conflict_type = str(item.get("conflict_type", "")).strip().lower()
        if conflict_type not in _VALID_CONFLICT_TYPES:
            conflict_type = ""
        if event in {"ADD", "NONE"}:
            # ADD/NONE 不是冲突，即使 LLM 无视提示词硬塞了一个 conflict_type
            # 也要清空——否则会污染 memory_history 的审计列，让离线按
            # conflict_type 分组统计冲突数的查询虚高。
            conflict_type = ""
        actions.append(
            {
                "event": event,
                "memory_id": str(item.get("target_memory_id", "")).strip(),
                "text": str(item.get("text", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "conflict_type": conflict_type,
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
            {
                "event": event, "memory_id": "", "text": text,
                "reason": "fallback", "conflict_type": "",
            }
        )
    return actions
