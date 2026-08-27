from __future__ import annotations

import re
from collections import defaultdict
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
_PATH_TOP_K = 10
# constraints.hops 的 schema 本身限定"最多2跳"（见
# app/agent/tools/structured_filter_query/tool.py 的 _PARAMETERS_SCHEMA），
# 路径搜索的跳数上限跟工具能表达的上限对齐——搜出工具用不了的3跳路径没有
# 意义。


def _tokenize_ngrams(text: str, *, max_len: int = _NGRAM_MAX_LEN) -> list[str]:
    """把 query 文本切成 token，再拼出 1~max_len 个 token 长的滑动窗口
    n-gram，作为跟候选名字比对的基本单位。用 dict.fromkeys 去重（保留
    顺序）——很多滑动窗口会拼出同一个子串，尤其是短/重复度高的
    query，去重能省掉后面重复的打分工作。"""
    tokens = _TOKEN_PATTERN.findall(text.lower())
    ngrams: list[str] = []
    for start in range(len(tokens)):
        for length in range(1, max_len + 1):
            end = start + length
            if end > len(tokens):
                break
            ngrams.append("".join(tokens[start:end]))
    return list(dict.fromkeys(ngrams))


def _longest_common_substring_length_prelowered(a: str, b: str) -> int:
    """跟 _longest_common_substring_length 逻辑完全一致，但假定调用方已经
    把 a/b 转成小写——热路径（_best_score 内层循环，可能对数万个候选
    各跑一遍）用这个版本跳过重复的 .lower() 调用；公开的
    longest_common_substring_score 仍然用会自己转小写的版本，签名/行为
    不变。"""
    best = 0
    for i in range(len(a)):
        for j in range(len(b)):
            k = 0
            while i + k < len(a) and j + k < len(b) and a[i + k] == b[j + k]:
                k += 1
            best = max(best, k)
    return best


def _longest_common_substring_length(a: str, b: str) -> int:
    return _longest_common_substring_length_prelowered(a.lower(), b.lower())


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


def _ngram_score_prelowered(ngram: str, lowered_name: str) -> float:
    """跟 longest_common_substring_score 打分逻辑一致，但假定 ngram/name
    已经是小写（_tokenize_ngrams 产出的 ngram 本来就已经小写，调用方对
    name 只转一次小写而不是对每个 ngram 都转一次）——_best_score 内层
    热循环专用。"""
    if not lowered_name:
        return 0.0
    overlap = _longest_common_substring_length_prelowered(ngram, lowered_name)
    if overlap < _MIN_OVERLAP_LENGTH:
        return 0.0
    return overlap / len(lowered_name)


def _best_score(query_text: str, ngrams: list[str], query_bigrams: set[str], *names: str) -> float:
    """ngrams 对多个候选名字（比如一个关系三元组的三个组成部分）分别
    打分，取最高的一个——命中任意一个组成部分就算这个候选跟 query
    相关，不要求全部命中。

    除了 n-gram 打分（受限于 n-gram 最长 _NGRAM_MAX_LEN 个 token，长名字
    必然算不出高分），额外做一次原始 query 文本的整段包含检查——只要
    候选名字整段出现在 query 里，不管候选名字多长都直接给满分，这是
    n-gram 逐段比对法结构性覆盖不到的场景（这个检查必须对每个候选无
    条件执行，不受下面的 bigram 预过滤影响，且开销是 Python 原生子串
    查找，不是这个函数真正昂贵的部分）。

    n-gram 打分路径本身在候选池很大时（数万个实体名）是这个函数的热
    点——对每个候选名字，先用 2-gram 集合跟 query_bigrams 做一次
    isdisjoint 判断：两个字符串如果没有任何公共的 2 字符子串，最长公共
    子串长度必然 < _MIN_OVERLAP_LENGTH，不可能在 n-gram 路径上及格，可以
    直接跳过后面 O(|ngram|·|name|) 的双重循环。这个预过滤只影响 n-gram
    打分路径，不影响上面的整段包含检查，也不会漏掉任何原本能打分及格
    的候选（详见调用方 recall_ontology_candidates 里 query_bigrams 的构造
    注释）。"""
    if not names:
        return 0.0
    lowered_query = query_text.lower()
    containment_score = max(
        (1.0 if len(name) >= _MIN_OVERLAP_LENGTH and name.lower() in lowered_query else 0.0)
        for name in names
    )
    if not ngrams:
        return containment_score
    ngram_score = 0.0
    for name in names:
        lowered_name = name.lower()
        name_bigrams = {lowered_name[i : i + 2] for i in range(len(lowered_name) - 1)}
        if name_bigrams.isdisjoint(query_bigrams):
            continue
        for ngram in ngrams:
            score = _ngram_score_prelowered(ngram, lowered_name)
            if score > ngram_score:
                ngram_score = score
    return max(ngram_score, containment_score)


def _rank(scored: list[tuple[float, object]], *, top_k: int) -> list[object]:
    kept = [(score, payload) for score, payload in scored if score >= _MIN_SCORE]
    kept.sort(key=lambda item: item[0], reverse=True)
    return [payload for _, payload in kept[:top_k]]


@dataclass(frozen=True)
class RecallPathHop:
    relation_type: str
    direction: str  # "outgoing" | "incoming"，跟 structured_filter_query_tool
    # 的 constraints.hops[].direction 用同一套取值，方便深层参数生成 LLM 直接
    # 照抄这个字段值，不用自己再判断方向。
    target_term_type: str


@dataclass(frozen=True)
class RecallPath:
    """从 source_term_type 出发、经过 hops 里每一跳，最终到达 hops 最后一个
    元素的 target_term_type 的一条完整可达路径——单跳关系已经由
    RecallCandidates.relations 覆盖，这里只收 2 跳（及以上，若未来放开
    _PATH_TOP_K 对应的跳数上限）的路径，因为那才是深层参数生成 LLM 自己
    推理最容易漏掉的部分（见 2026-08-27 对"公司有多少个订单"这类跨中间
    实体查询的排查记录）。"""
    source_term_type: str
    hops: tuple[RecallPathHop, ...]


def _build_adjacency(
    allowed_combinations: list[AllowedCombination],
) -> dict[str, list[RecallPathHop]]:
    """把 allowed_combinations 铺成的关系三元组，展开成一张按 term_type
    索引、双向都能走的邻接表——一条 "订单号 --BELONG_TO--> 产品" 的确认
    组合，既要能从"订单号"正向走到"产品"（direction=outgoing），也要能
    支持将来某条路径需要从"产品"反向走回"订单号"（direction=incoming）
    的场景，跟 structured_filter_query_tool 的 constraints.hops 本身允许
    两个方向是同一个道理。"""
    adjacency: dict[str, list[RecallPathHop]] = defaultdict(list)
    for combo in allowed_combinations:
        adjacency[combo.subject_term_type].append(
            RecallPathHop(
                relation_type=combo.relation_type,
                direction="outgoing",
                target_term_type=combo.object_term_type,
            )
        )
        adjacency[combo.object_term_type].append(
            RecallPathHop(
                relation_type=combo.relation_type,
                direction="incoming",
                target_term_type=combo.subject_term_type,
            )
        )
    return adjacency


def _find_multi_hop_paths(
    *,
    source_term_types: list[str],
    target_term_types: set[str],
    allowed_combinations: list[AllowedCombination],
    max_hops: int = 2,
) -> list[RecallPath]:
    """从每个候选 source_term_type 出发，在 allowed_combinations 构成的
    关系图上做有界 BFS，找出能到达任一 target_term_type、长度在
    [2, max_hops] 跳之间的路径——只找多跳路径，1 跳的直接关系已经由
    RecallCandidates.relations 单独覆盖，不在这里重复。

    source/target 都来自同一批"看起来跟这次查询相关"的候选 term_type
    （见 recall_ontology_candidates 的调用处），不是全量本体节点，规模
    小（通常个位数到十位数），双重循环 BFS 不构成性能问题。
    """
    adjacency = _build_adjacency(allowed_combinations)
    paths: list[RecallPath] = []
    seen: set[tuple[str, tuple[RecallPathHop, ...]]] = set()

    for source in source_term_types:
        frontier: list[tuple[str, tuple[RecallPathHop, ...]]] = [(source, ())]
        for _ in range(max_hops):
            next_frontier: list[tuple[str, tuple[RecallPathHop, ...]]] = []
            for current_type, hops_so_far in frontier:
                for hop in adjacency.get(current_type, ()):
                    new_hops = hops_so_far + (hop,)
                    if (
                        len(new_hops) >= 2
                        and hop.target_term_type in target_term_types
                        and hop.target_term_type != source
                    ):
                        key = (source, new_hops)
                        if key not in seen:
                            seen.add(key)
                            paths.append(RecallPath(source_term_type=source, hops=new_hops))
                    next_frontier.append((hop.target_term_type, new_hops))
            frontier = next_frontier

    return paths[:_PATH_TOP_K]


@dataclass(frozen=True)
class RecallCandidates:
    term_types: list[str]
    relations: list[AllowedCombination]
    fields: list[tuple[str, str]]  # (term_type, field_name)
    paths: list[RecallPath]
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
    # 所有 ngram 里出现过的 2 字符子串的并集。任何候选名字如果跟某个
    # ngram 的最长公共子串 >= _MIN_OVERLAP_LENGTH(2)，那两者一定共享至少
    # 一个 2 字符子串，而这个子串必然是该 ngram 的一个 2-gram、因而必然
    # 落在这个并集里——所以用它对候选名字做 isdisjoint 预过滤，绝不会
    # 漏掉任何原本能在 n-gram 路径上打分及格的候选，只是提前排除掉注定
    # 打不出及格分的大多数候选，跳过后面昂贵的双重循环。
    query_bigrams = {ngram[i : i + 2] for ngram in ngrams for i in range(len(ngram) - 1)}

    term_types = _rank(
        [(_best_score(query_text, ngrams, query_bigrams, name), name) for name in term_type_schema],
        top_k=_TERM_TYPE_TOP_K,
    )
    relations = _rank(
        [
            (
                _best_score(
                    query_text, ngrams, query_bigrams,
                    combo.subject_term_type, combo.relation_type, combo.object_term_type,
                ),
                combo,
            )
            for combo in allowed_combinations
        ],
        top_k=_RELATION_TOP_K,
    )
    fields = _rank(
        [
            (_best_score(query_text, ngrams, query_bigrams, field.name), (term_type, field.name))
            for term_type, category in term_type_schema.items()
            for field in category.extra_fields
        ],
        top_k=_FIELD_TOP_K,
    )
    entities = _rank(
        [(_best_score(query_text, ngrams, query_bigrams, term.standard_name), term) for term in terms],
        top_k=_ENTITY_TOP_K,
    )

    # 多跳路径搜索的起点/终点都取自"已经独立判定为跟这次查询相关"的候选
    # term_type——source 是候选 term_type 本身；target 在此基础上并上候选
    # 实体各自的 term_type（比如查询里直接点了"Coca-Cola"这个实体名，但
    # "公司"这个 term_type 本身没有单独获得足够高的字面相似度分数时，仍然
    # 能通过它命中的实体反推出目标类型）。source 和 target 用同一个候选
    # term_type 集合互相查找路径——不要求提前区分"谁是起点谁是终点"，
    # BFS 本身会两边都试。
    target_term_types = set(term_types) | {term.term_type for term in entities}
    paths = _find_multi_hop_paths(
        source_term_types=term_types,
        target_term_types=target_term_types,
        allowed_combinations=allowed_combinations,
    )

    return RecallCandidates(
        term_types=term_types, relations=relations, fields=fields, paths=paths, entities=entities,
    )


def format_recall_candidates(candidates: RecallCandidates) -> str:
    """把召回结果格式化成人类可读的文本块，塞进独立参数生成调用的 prompt。"""
    lines: list[str] = []
    if candidates.term_types:
        lines.append("可能相关的实体类型：" + "、".join(candidates.term_types))
    if candidates.relations:
        lines.append("可能相关的关系（方向：subject --relation_type--> object）：")
        for combo in candidates.relations:
            lines.append(f"  - {combo.subject_term_type} --{combo.relation_type}--> {combo.object_term_type}")
    if candidates.paths:
        # 起点和目标类型之间没有直接关系、要跨一个中间实体类型才能连起来的
        # 查询（比如"公司有多少个订单"要经过"产品"这个中间类型），最容易
        # 让深层参数生成 LLM 自己漏掉中间那一跳——这里把完整路径拼好递过去，
        # 不要求它自己从上面摊平的单跳关系列表里推理拼接。箭头方向跟
        # constraints.hops[].direction 的取值对应：--relation_type--> 是
        # outgoing，<--relation_type-- 是 incoming，可以直接照抄。
        lines.append("可能相关的多跳路径（起点和目标类型之间要跨中间类型才能连上）：")
        for path in candidates.paths:
            segments = [path.source_term_type]
            for hop in path.hops:
                arrow = (
                    f"--{hop.relation_type}-->"
                    if hop.direction == "outgoing"
                    else f"<--{hop.relation_type}--"
                )
                segments.append(arrow)
                segments.append(hop.target_term_type)
            lines.append("  - " + " ".join(segments))
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
