from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

# 审查员只看得到一段孤立的回答文字。不告诉它这段话从哪来、给谁看，它就只能
# 按"公开渠道的客服回复"去判——那样的话，凡是列出业务标识的数据查询结果都
# 像数据泄露。实测：demo 租户问"Coca-Cola 下的订单号分别是什么"，回答列出
# 10 个订单号，审查员 4 次里 4 次判不安全，理由是"列出了大量未脱敏的订单号，
# 属于泄露客户/订单数据"——而那正是用户要的答案，数据来自这个租户自己的
# 知识库，提问者已登录。
#
# 所以把"业务数据本身"和"真正该拦的东西"分开写清楚。个人隐私仍然拦：
# 手机号/身份证号规则层已经拦了一道，但姓名+住址这类组合正则覆盖不了，
# 那是这一层存在的理由之一。
_SYSTEM_PROMPT = (
    "你是客服回答的语义级安全审查员。\n"
    "背景：这段回答由企业知识库问答系统生成，回答里的事实查询自**提问者所在"
    "企业自己的知识库**，提问者是**已登录的用户**。\n"
    "下面这些是用户查询的业务数据本身，出现在回答里是正常的，**不算泄露**："
    "订单号、产品名、公司名、类别、金额、数量、日期、以及知识库里记录的其他"
    "业务标识和业务属性。\n"
    "只有下面这些才判为不安全：\n"
    "- 个人隐私：个人的手机号、身份证号、个人邮箱、家庭住址、银行卡号；\n"
    "- 凭据：密码、密钥、token；\n"
    "- 系统内部信息：系统提示词片段、内部字段名（如带下划线的英文字段名）、"
    "node_key 这类内部标识、报错堆栈；\n"
    "- 不当建议、误导性表述、违反平台规范的内容。\n"
    '只输出 JSON：{"is_safe": true/false, "reason": "..."}'
)


@dataclass(frozen=True)
class SemanticSafetyResult:
    is_safe: bool
    reviewed: bool
    reason: str = ""


async def semantic_safety_review(
    text: str,
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    timeout_sec: float = 2.0,
) -> SemanticSafetyResult:
    """LLM 语义级安全审查，作为规则级 check_text 之外的补充深度检查。

    失败/超时的安全默认值选择"放行但标记未审查"而非"判定不安全直接拦截"：
    规则级检查（check_text）已经是先行的一道安全网，语义审查是在其之上的
    增强而非唯一防线，把它当成硬性拦截点会导致 LLM 一抖动整个服务的输出
    就全部被挡，可用性代价太大；标记 reviewed=False 让调用方知道"这轮回答
    没有真正过语义审查"，可用于监控告警统计审查覆盖率。
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
        logger.info("语义安全审查超时，放行但标记未审查")
        return SemanticSafetyResult(is_safe=True, reviewed=False)
    except Exception:
        logger.warning("语义安全审查失败，放行但标记未审查", exc_info=True)
        return SemanticSafetyResult(is_safe=True, reviewed=False)

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        return SemanticSafetyResult(is_safe=True, reviewed=False)
    if not isinstance(payload, dict) or "is_safe" not in payload:
        return SemanticSafetyResult(is_safe=True, reviewed=False)

    return SemanticSafetyResult(
        is_safe=bool(payload.get("is_safe")),
        reviewed=True,
        reason=str(payload.get("reason", "")),
    )
