from __future__ import annotations

import asyncio
import json
import logging

from app.graphrag.ontology_constraints import AllowedCombination
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# system prompt 不再硬编码通用关系类型，而是按调用方传入的该租户已确认
# （status="confirmed"）本体——关系类型/实体类型/允许组合——动态拼接，
# 抽取严格限定在这个封闭范围内。见
# docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 E。
_SYSTEM_PROMPT_TEMPLATE = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系，但只能抽取下面明确列出的"
    "实体类型和关系类型，不能抽取范围之外的内容——这是这个租户在本体 "
    "schema 里已经确认好的封闭定义，不是建议，是硬约束。\n"
    "允许的实体类型（subject_type/object_type 只能是这些值之一）：\n"
    "{term_types}\n"
    "允许的关系类型（relation_type 只能是这些值之一）：\n"
    "{relation_types}\n"
    "允许的（主体类型, 关系类型, 客体类型）三元组组合，subject_type/"
    "relation_type/object_type 的组合必须命中下面某一行，命中不了就不要"
    "输出这条关系：\n"
    "{allowed_combinations}\n"
    '只输出 JSON：{{"relations":[{{"subject":"...","subject_type":"...",'
    '"object":"...","object_type":"...","relation_type":"...",'
    '"evidence":"..."}}]}}。subject_type/object_type 分别是 subject/object '
    "这两个专有名词各自的实体类型，必须是上面允许的实体类型之一。"
    "evidence 是原文里支持这条关系的一句话原文摘录，给人工审核用，必须是"
    "原文摘录、不能改写或概括；实在找不到能直接引用的完整单句时，摘取最"
    "贴近的一小段原文，不要留空。"
    "不确定的内容不要编造，抽不出符合上述范围的关系就返回空列表。"
    "如果输入包含多个用 [片段N] 标记分隔的片段，只抽取同一个片段内部出现的"
    "关系，不要把不同片段里的实体强行关联起来。"
)


def _build_system_prompt(
    *, relation_types: list[str], term_types: list[str],
    allowed_combinations: list[AllowedCombination],
) -> str:
    combos_text = "\n".join(
        f"- {c.subject_term_type} {c.relation_type} {c.object_term_type}"
        for c in allowed_combinations
    ) or "（无——该租户尚未配置任何允许组合，本次不会抽取出任何关系）"
    return _SYSTEM_PROMPT_TEMPLATE.format(
        term_types="、".join(term_types) or "（无）",
        relation_types="、".join(relation_types) or "（无）",
        allowed_combinations=combos_text,
    )


def _build_user_content(segments: list[str]) -> str:
    """单片段时直接发原文，不加标记（兼容单片段场景的 prompt 简洁性）；
    多片段时用 [片段N] 标记分隔，配合 system prompt 里的"不跨片段关联"
    指令，防止批量抽取时 LLM 把毫不相关的片段内容编造成跨片段关系。
    """
    if len(segments) == 1:
        return segments[0]
    return "\n\n".join(
        f"[片段{i}]\n{segment}" for i, segment in enumerate(segments, start=1)
    )


async def extract_candidate_relations(
    segments: list[str],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    relation_types: list[str],
    term_types: list[str],
    allowed_combinations: list[AllowedCombination],
    timeout_sec: float = 30.0,
) -> list[dict[str, str]]:
    """LLM 抽取候选关系；失败/超时/JSON 解析失败均回退空列表，不阻塞摄取流程。

    relation_types/term_types/allowed_combinations 是该租户当前已确认
    （status="confirmed"）的本体 schema——调用方（见 graph_extraction.py）
    负责查出这三份列表再传进来，这个函数本身不碰数据库。抽取严格限定在
    这个范围内，不再是过去硬编码的 10 种通用关系类型，见
    docs/superpowers/specs/2026-08-19-data-entry-unification-design.md 决策 E。

    segments 支持一次传入多个 chunk 的文本，合并成一次 LLM 调用（见
    graph_extraction.py 的攒批逻辑）——关系写入只按整篇文档 source 溯源，
    不依赖 chunk 粒度，合并调用是纯效率提升。

    timeout_sec 默认 30 秒：这是后台摄取任务专用的默认值，比项目里"实时
    对话链路"惯用的 2 秒宽松得多——摄取没有用户在等，用更长的超时换取
    更高的抽取成功率是合算的。

    这里只产出"候选"，尚未与术语表归一化对齐——归一化在
    normalize_candidate_relations 中完成，二者分开是为了保持每个
    函数职责单一，便于分别测试。
    """
    if not segments:
        return []

    system_prompt = _build_system_prompt(
        relation_types=relation_types, term_types=term_types,
        allowed_combinations=allowed_combinations,
    )
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": _build_user_content(segments)},
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
        subject_type = str(item.get("subject_type", "")).strip()
        object_type = str(item.get("object_type", "")).strip()
        evidence = str(item.get("evidence") or "").strip()
        if subject and obj and relation_type:
            relations.append(
                {
                    "subject": subject,
                    "object": obj,
                    "relation_type": relation_type,
                    "subject_type": subject_type,
                    "object_type": object_type,
                    "evidence": evidence,
                }
            )
    return relations
