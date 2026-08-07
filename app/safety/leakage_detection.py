from __future__ import annotations

import re
from dataclasses import dataclass, field

# 只覆盖结构性特征明显、误报率低的内部信息泄露特征——刻意不做泛化的
# "密码/密钥/token"关键词匹配：中文客服问答场景里"密码"是高频正常业务
# 词（"请重置密码"、"密码至少8位"），这类关键词正则误报率会很高，交给
# semantic_safety_review 的语义审查层判断更合适。这和 prompt_injection.py
# "规则兜底、不追求完备"的设计取向一致。
_LEAKAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "stack_trace",
        re.compile(r'Traceback \(most recent call last\)|File "[^"]+\.py", line \d+'),
    ),
    (
        "internal_file_path",
        re.compile(r"\bapp/[\w./]*\.py\b"),
    ),
    (
        "internal_env_var",
        re.compile(r"\bCUSTOMER_RAG_[A-Z_]+\b"),
    ),
    (
        "db_query_fragment",
        re.compile(r"\bMATCH\s*\(|\bCREATE\s*\(|\bSELECT\s+.+?\s+FROM\b", re.IGNORECASE),
    ),
]


@dataclass(frozen=True)
class LeakageDetectionResult:
    is_leaked: bool
    matched_categories: list[str] = field(default_factory=list)


def detect_internal_leakage(text: str) -> LeakageDetectionResult:
    """规则级检测输出文本里典型的内部数据泄露特征。

    是 output_safety_node 里 check_text 之外的并列规则层，命中即和
    check_text 一样短路拦截，不进入更贵的 semantic_safety_review。
    """
    matched = [name for name, pattern in _LEAKAGE_PATTERNS if pattern.search(text)]
    return LeakageDetectionResult(is_leaked=bool(matched), matched_categories=matched)
