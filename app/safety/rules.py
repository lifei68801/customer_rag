from __future__ import annotations

import re
from dataclasses import dataclass, field

_PHONE_NUMBER_PATTERN = re.compile(r"1[3-9]\d{9}")
# 18 位中国大陆身份证号：17 位数字 + 末位数字或大小写 X 校验位。和手机号
# 正则一样不做完整性校验（不验证省份码/生日合法性/校验位算法），只做
# 结构性识别——客服场景够用，过度校验会增加复杂度但不提升实际拦截效果。
_ID_CARD_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 标准 email 格式，不追求穷尽 RFC 5322 的所有合法形式。
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# 输入/输出安全网命中时的兜底话术。原定义在 app/agent/graph.py，现搬到
# 这里作为共享位置：Task 4 的 app/qa/answer.py 需要同一份文案，此前两处
# 各写一份有文案不一致的风险。
UNSAFE_INPUT_MESSAGE = "您的问题包含无法处理的敏感内容，请修改后重新提问。"
UNSAFE_OUTPUT_MESSAGE = "抱歉，生成的回答未通过安全审查，已为您转接人工客服。"

# 静态检索路径/Planner 路径流式输出时，单句命中轻量规则检查（check_text）
# 后的兜底文案——跟 UNSAFE_INPUT_MESSAGE/UNSAFE_OUTPUT_MESSAGE 是同一类
# "安全网命中时的兜底话术"，放在这个共享位置的理由也一样：原定义在
# app/agent/graph.py，2026-08-23 起 app/agent/planner.py 的流式变体也
# 需要同一份文案，两处各自定义会有文案不一致的风险。
LITE_SAFETY_FALLBACK_SENTENCE = "（该部分内容因安全检查被过滤。）"


@dataclass(frozen=True)
class SafetyCheckResult:
    is_safe: bool
    matched_terms: list[str] = field(default_factory=list)


def check_text(
    text: str,
    *,
    banned_terms: list[str] | None = None,
    include_email: bool = True,
) -> SafetyCheckResult:
    """规则级安全检测：手机号/身份证号始终检测；邮箱默认检测，但输出侧/
    语音侧调用需要传 include_email=False——知识库文档里的联系邮箱
    （如"请联系 support@xxx.com"）经常会原样出现在 LLM 生成的回答里，
    在输出侧拦截这类内容是误伤，只有输入侧（防止客户提交自己的邮箱
    这类 PII）需要这条规则。
    """
    matched: list[str] = []
    if _PHONE_NUMBER_PATTERN.search(text):
        matched.append("phone_number")
    if _ID_CARD_PATTERN.search(text):
        matched.append("id_card")
    if include_email and _EMAIL_PATTERN.search(text):
        matched.append("email")
    for term in banned_terms or []:
        if term in text:
            matched.append(term)
    return SafetyCheckResult(is_safe=not matched, matched_terms=matched)
