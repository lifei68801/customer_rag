from __future__ import annotations

import asyncio
import difflib
import json
import logging

from app.graphrag.ontology import Term
from app.providers.base import ProviderCapability, ProviderRequest
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是语音识别专有名词校正器。"
    "下面给出若干候选片段及其疑似对应的标准术语，请判断每个候选是否应该"
    "替换为标准术语（只有确信是同音/形近误识别时才替换，不确定就不替换）。"
    '只输出 JSON：{"replacements":[{"span":"...","standard_name":"...","replace":true/false}]}'
)


def _find_fuzzy_candidates(
    text: str, terms: list[Term], *, threshold: float
) -> list[tuple[str, str]]:
    """滑动窗口 + difflib 相似度，找出"疑似专有名词但未精确命中"的片段。

    比较一律在小写归一后进行——ASR 转写出的英文大小写本来就不可靠，用
    大小写差异去扣相似度分没有任何意义，只会让 "COCA-COLA" 这种全大写
    转写既过不了"整段已命中"的短路（`alias in text`），又在 difflib 上
    掉到阈值以下，两头都不命中。这跟项目里其余字符匹配环节
    （term_matcher、ontology_recall、ontology.resolve_term）保持一致。

    返回的 span 保留原文大小写：调用方 correct_asr_terms 拿它做字面
    `text.replace(span, standard_name)`，归一化过的 span 会 replace 不中。
    """
    text_lower = text.lower()
    candidates: list[tuple[str, str]] = []
    seen_spans: set[str] = set()
    for term in terms:
        for alias in [term.standard_name, *term.aliases]:
            if not alias or alias.lower() in text_lower:
                continue
            alias_lower = alias.lower()
            window = len(alias)
            for i in range(len(text) - window + 1):
                span = text[i : i + window]
                if span in seen_spans:
                    continue
                ratio = difflib.SequenceMatcher(None, span.lower(), alias_lower).ratio()
                if ratio >= threshold:
                    candidates.append((span, term.standard_name))
                    seen_spans.add(span)
    return candidates


async def correct_asr_terms(
    text: str,
    *,
    terms: list[Term],
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    fuzzy_threshold: float = 0.6,
    timeout_sec: float = 2.0,
) -> str:
    """ASR 转写文本的专有名词校正：模糊匹配术语表找候选，LLM 判断是否替换。

    未找到任何模糊候选时直接跳过（不调用 LLM，节省延迟）；LLM 失败/超时/
    解析失败时保留原始文本不做替换——校正失败的安全默认值是"不动"，
    不是"猜一个"。
    """
    candidates = _find_fuzzy_candidates(text, terms, threshold=fuzzy_threshold)
    if not candidates:
        return text

    payload = json.dumps(
        {"candidates": [{"span": span, "standard_name": name} for span, name in candidates]},
        ensure_ascii=False,
    )
    try:
        result = await asyncio.wait_for(
            llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": f"原文：{text}\n{payload}"},
                    ]
                ),
                provider_name=llm_provider_name,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("ASR 术语校正超时，保留原文")
        return text
    except Exception:
        logger.warning("ASR 术语校正失败，保留原文", exc_info=True)
        return text

    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError:
        return text
    replacements = parsed.get("replacements") if isinstance(parsed, dict) else None
    if not isinstance(replacements, list):
        return text

    corrected = text
    for item in replacements:
        if not isinstance(item, dict) or not item.get("replace"):
            continue
        span = str(item.get("span", ""))
        standard_name = str(item.get("standard_name", ""))
        if span and standard_name:
            corrected = corrected.replace(span, standard_name)
    return corrected
