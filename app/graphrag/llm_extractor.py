from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系。"
    '只输出 JSON：{"relations":[{"subject":"...","object":"...","relation_type":"RELATED_TO"}]}。'
    "relation_type 仅允许 RELATED_TO 或 BELONGS_TO_MODULE。"
    "不确定的内容不要编造，抽不出关系就返回空列表。"
)


async def extract_candidate_relations(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> list[dict[str, str]]:
    """LLM 抽取候选关系；失败/超时/JSON 解析失败均回退空列表，不阻塞摄取流程。

    这里只产出"候选"，尚未与术语表归一化对齐——归一化在
    normalize_candidate_relations 中完成，二者分开是为了保持每个
    函数职责单一，便于分别测试。
    """
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("关系抽取超时，回退空列表")
        return []
    except Exception:
        logger.warning("关系抽取失败，回退空列表", exc_info=True)
        return []

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        logger.warning("关系抽取返回非 JSON，回退空列表")
        return []

    raw = payload.get("relations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []

    relations: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        obj = str(item.get("object", "")).strip()
        relation_type = str(item.get("relation_type", "")).strip()
        if subject and obj and relation_type:
            relations.append(
                {"subject": subject, "object": obj, "relation_type": relation_type}
            )
    return relations
