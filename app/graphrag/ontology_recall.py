from __future__ import annotations

import re
from dataclasses import dataclass

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination

# 跟 app/retrieval/bm25.py 的 _TOKEN_PATTERN 用同一套规则（英文按
# [a-z0-9_]+ 整段切、中文按字切），这里复制这一行正则常量而不是跨模块
# import 一个下划线开头的私有名字——两边各自独立维护同一份简单规则，
# 比引入模块间私有耦合更清晰。
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[一-鿿]")

_MIN_SCORE = 0.3
_MIN_OVERLAP_LENGTH = 2
_NGRAM_MAX_LEN = 4
_TERM_TYPE_TOP_K = 10
_RELATION_TOP_K = 10
_FIELD_TOP_K = 10
_ENTITY_TOP_K = 20


def _tokenize_ngrams(text: str, *, max_len: int = _NGRAM_MAX_LEN) -> list[str]:
    """把 query 文本切成 token，再拼出 1~max_len 个 token 长的滑动窗口
    n-gram，作为跟候选名字比对的基本单位。"""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    ngrams: list[str] = []
    for start in range(len(tokens)):
        for length in range(1, max_len + 1):
            end = start + length
            if end > len(tokens):
                break
            ngrams.append("".join(tokens[start:end]))
    return ngrams


def _longest_common_substring_length(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def longest_common_substring_score(a: str, b: str) -> float:
    """最长公共连续子串长度（大小写不敏感）除以 b 的长度，归一化成 0~1
    分数——a 是 query 里切出来的 n-gram，b 是候选名字。重叠长度小于
    _MIN_OVERLAP_LENGTH 个字符时直接返回0，避免单字符/极短噪声匹配
    （否则短候选名字下归一化分数会虚高）。"""
    if not b:
        return 0.0
    overlap = _longest_common_substring_length(a, b)
    if overlap < _MIN_OVERLAP_LENGTH:
        return 0.0
    return overlap / len(b)


def _best_score(ngrams: list[str], *names: str) -> float:
    """ngrams 对多个候选名字（比如一个关系三元组的三个组成部分）分别
    打分，取最高的一个——命中任意一个组成部分就算这个候选跟 query
    相关，不要求全部命中。"""
    if not ngrams:
        return 0.0
    return max(
        (longest_common_substring_score(ngram, name) for ngram in ngrams for name in names),
        default=0.0,
    )


def _rank(scored: list[tuple[float, object]], *, top_k: int) -> list[object]:
    kept = [(score, payload) for score, payload in scored if score >= _MIN_SCORE]
    kept.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in kept[:top_k]]


@dataclass(frozen=True)
class RecallCandidates:
    term_types: list[str]
    relations: list[AllowedCombination]
    fields: list[tuple[str, str]]  # (term_type, field_name)
    entities: list[Term]


def recall_ontology_candidates(
    query_text: str,
    *,
    terms: list[Term],
    term_type_schema: dict[str, TermTypeCategory],
    allowed_combinations: list[AllowedCombination],
) -> RecallCandidates:
    """针对 query_text，从本体的四类信息里各自召回最相关的候选，供独立
    参数生成调用参考——term_type/relation 三元组/字段名池子通常很小，
    召回时会把自己基本全部召回回来；实体名池子可能很大（数万条），
    真正需要靠打分+截断收窄候选范围。"""
    ngrams = _tokenize_ngrams(query_text)

    term_types = _rank(
        [(_best_score(ngrams, name), name) for name in term_type_schema],
        top_k=_TERM_TYPE_TOP_K,
    )
    relations = _rank(
        [
            (
                _best_score(ngrams, combo.subject_term_type, combo.relation_type, combo.object_term_type),
                combo,
            )
            for combo in allowed_combinations
        ],
        top_k=_RELATION_TOP_K,
    )
    fields = _rank(
        [
            (_best_score(ngrams, field.name), (term_type, field.name))
            for term_type, category in term_type_schema.items()
            for field in category.extra_fields
        ],
        top_k=_FIELD_TOP_K,
    )
    entities = _rank(
        [(_best_score(ngrams, term.standard_name), term) for term in terms],
        top_k=_ENTITY_TOP_K,
    )

    return RecallCandidates(term_types=term_types, relations=relations, fields=fields, entities=entities)


def format_recall_candidates(candidates: RecallCandidates) -> str:
    """把召回结果格式化成人类可读的文本块，塞进独立参数生成调用的 prompt。"""
    lines: list[str] = []
    if candidates.term_types:
        lines.append("可能相关的实体类型：" + "、".join(candidates.term_types))
    if candidates.relations:
        lines.append("可能相关的关系（方向：subject --relation_type--> object）：")
        for combo in candidates.relations:
            lines.append(f"  - {combo.subject_term_type} --{combo.relation_type}--> {combo.object_term_type}")
    if candidates.fields:
        lines.append("可能相关的字段：")
        for term_type, field_name in candidates.fields:
            lines.append(f"  - {term_type}.{field_name}")
    if candidates.entities:
        lines.append("可能相关的已知实体（标准名/类型）：")
        for term in candidates.entities:
            lines.append(f"  - {term.standard_name}（{term.term_type}）")
    if not lines:
        return "（本体里没有召回到明显相关的候选，请谨慎作答，字段/关系名要用已知的、不要凭空发明）"
    return "\n".join(lines)
