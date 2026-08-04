from __future__ import annotations

import re
from dataclasses import dataclass, field

_PHONE_NUMBER_PATTERN = re.compile(r"1[3-9]\d{9}")


@dataclass(frozen=True)
class SafetyCheckResult:
    is_safe: bool
    matched_terms: list[str] = field(default_factory=list)


def check_text(
    text: str,
    *,
    banned_terms: list[str] | None = None,
) -> SafetyCheckResult:
    matched: list[str] = []
    if _PHONE_NUMBER_PATTERN.search(text):
        matched.append("phone_number")
    for term in banned_terms or []:
        if term in text:
            matched.append(term)
    return SafetyCheckResult(is_safe=not matched, matched_terms=matched)
