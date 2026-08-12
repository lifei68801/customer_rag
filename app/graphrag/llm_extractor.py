from __future__ import annotations

import asyncio
import json
import logging

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 10 种跨领域通用拓扑关系，每种配一个极简中文示例短语（不是完整
# few-shot 例句）帮助 LLM 锚定语义边界，同时把每次调用的固定 token
# 开销控制在可接受范围——见 docs/superpowers/specs/2026-08-09-
# chunking-graph-extraction-redesign-design.md 第 3.2/4 节。
_SYSTEM_PROMPT = (
    "你是知识图谱关系抽取器。"
    "请从给定文档片段中抽取专有名词之间的关系。"
    '专有名词指在这门具体生意里有实际业务含义、值得单独作为图谱节点的'
    '名词或短语：产品/型号名（如"大床房"）、编号（如"302号房"）、地点名'
    '（如"三楼健身房"）、机构/品牌名（如"某连锁酒店"）、职务/角色头衔'
    '（如"值班经理"），也包括具体的业务流程、状态、动作、活动名称（如'
    '"入住登记""房间异味""更换房间""促销活动"——这些虽然语法上是动作/'
    '状态/类别词，但本身就是这门生意的业务术语，relation_type 里 IS_A/'
    'PART_OF/PRECEDES/ADDRESSED_BY/RELATED_TO 的例子都依赖这类词）；不要'
    '抽取脱离具体业务场景、换成任何行业都通用的空泛填充词，例如孤立出现'
    '的"设备""问题""服务""顾客""流程"这类没有具体指代对象的泛称。'
    '只输出 JSON：{"relations":[{"subject":"...","object":"...","relation_type":"RELATED_TO"}]}。'
    "relation_type 仅允许以下 10 种，每种给一个例子帮助理解：\n"
    'RELATED_TO（兜底弱关联，如"促销活动 RELATED_TO 会员日"）、\n'
    'PART_OF（部分-整体，如"客房 PART_OF 酒店"）、\n'
    'IS_A（类别从属，如"大床房 IS_A 客房"）、\n'
    'REQUIRES（前提依赖，如"预订套餐 REQUIRES 会员资格"）、\n'
    'ALTERNATIVE_TO（替代/类似，如"标准间 ALTERNATIVE_TO 大床房"）、\n'
    'CAUSES（因果，如"恶劣天气 CAUSES 接送延误"）、\n'
    'ADDRESSED_BY（问题由方案解决，如"房间异味 ADDRESSED_BY 更换房间"）、\n'
    'LOCATED_IN（空间/组织归属，如"健身房 LOCATED_IN 三楼"）、\n'
    'APPLIES_TO（适用范围，如"会员折扣 APPLIES_TO 非节假日预订"）、\n'
    'PRECEDES（流程先后，如"入住登记 PRECEDES 领取房卡"）。\n'
    "不确定的内容不要编造，抽不出关系就返回空列表。"
    "如果输入包含多个用 [片段N] 标记分隔的片段，只抽取同一个片段内部出现的"
    "关系，不要把不同片段里的实体强行关联起来。"
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
    timeout_sec: float = 30.0,
) -> list[dict[str, str]]:
    """LLM 抽取候选关系；失败/超时/JSON 解析失败均回退空列表，不阻塞摄取流程。

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

    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
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
        if subject and obj and relation_type:
            relations.append(
                {"subject": subject, "object": obj, "relation_type": relation_type}
            )
    return relations
