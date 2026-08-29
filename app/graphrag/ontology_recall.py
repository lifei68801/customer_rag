from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.term_matcher import matched_length

_MIN_SCORE = 0.3
# 实体召回单独用更高的 precision 门槛。_MIN_SCORE 的 0.3 是给旧的"最长连续
# 公共子串 + 最小重叠2字符"打分器定的——那时候要拿到 0.3 得有一段连续覆盖
# 候选名 30% 的子串。换成带间隔约束的子序列打分后，0.3 靠巧合就能达到：
# 实测 query "查询Coca-Cola这家公司名下有多少个订单" 下，无关人名
# Alice 拿 0.4000、Paul Cole 0.4444、Nicole Le 0.3333（都是在 "coca-cola"
# 里凑到一两个字符），而真正被提到的实体是 1.0000，打错字的
# coke-cola->Coca-Cola 也有 0.7778。0.6 落在这两群中间。
# 本体词汇（term_type/relation/field）仍然用 _MIN_SCORE：那些名字短、
# 候选池小，0.3 在那里没有这个问题（实测"订单号" 0.6667、"公司" 1.0）。
_ENTITY_MIN_SCORE = 0.6
_MIN_OVERLAP_LENGTH = 2
_TERM_TYPE_TOP_K = 10
_RELATION_TOP_K = 10
_FIELD_TOP_K = 10
_ENTITY_TOP_K = 20
_PATH_TOP_K = 10
# constraints.hops 的 schema 本身限定"最多2跳"（见
# app/agent/tools/structured_filter_query/tool.py 的 _PARAMETERS_SCHEMA），
# 路径搜索的跳数上限跟工具能表达的上限对齐——搜出工具用不了的3跳路径没有
# 意义。
_FBETA = 0.5


def _longest_common_substring_length_prelowered(a: str, b: str) -> int:
    """跟 _longest_common_substring_length 逻辑完全一致，但假定调用方已经
    把 a/b 转成小写，省掉重复的 .lower() 调用；公开的
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


def _matched_chars(query: str, name: str) -> int:
    """候选名 name 的字符按顺序在 query 里能匹配上多少个。

    整段包含时直接按满额算——整段命中是最强的信号，不该因为候选名内部
    字符间隔而被 matched_length 的间隔约束打折。
    """
    if not name or not query:
        return 0
    if name.lower() in query.lower():
        return len(name)
    return matched_length(query, name)


def precision_match_score(query: str, name: str) -> float:
    """候选名被 query 覆盖了多少（单向精确率）。跟 query 长度无关，所以这是
    绝对及格线 _MIN_SCORE 唯一合适的判据——见 fbeta_match_score 的说明。"""
    matched = _matched_chars(query, name)
    return matched / len(name) if matched else 0.0


def fbeta_match_score(query: str, name: str, *, beta: float = _FBETA) -> float:
    """双向 F-beta：P=匹配长度/len(name)，R=匹配长度/len(query)。

    只用于【排序】，绝不用于绝对及格线。R 项带着 len(query)，所以同一个
    实体会随 query 变长而掉分——实测整段命中的情况下，len(query) 超过
    12.67*len(name) 就会跌破 _MIN_SCORE(0.3)，而生产调用方拼出来的
    query_text 常有 60-120 字符，足以让 4 字实体整个消失。排序是相对的，
    这个长度敏感性无害；及格线是绝对的，就致命。

    存在的理由仍然是"短候选名虚高"：`Cola`(4字) 是 `Coca-Cola`(9字) 的子串，
    只看 P 的话 Cola 永远拿满分压过 Coca-Cola——这正是 2026-08-27
    "coke-cola公司有多少个订单" 召回到错误实体的成因。beta=0.5 实测：
    beta=0.2 时 Cola 0.8889 仍高于 Coca-Cola 0.7521；beta=0.5 时
    Coca-Cola 0.6604 反超 Cola 0.6061。
    """
    matched = _matched_chars(query, name)
    if not matched:
        return 0.0
    precision = matched / len(name)
    recall = matched / len(query)
    b2 = beta * beta
    return (1 + b2) * precision * recall / (b2 * precision + recall)


def _best_scores(
    query_text: str, query_chars: set[str], *names: str, use_fbeta: bool = False
) -> tuple[float, float]:
    """对多个候选名（比如一个关系三元组的三个组成部分）分别打分，各取最好的
    一个，返回 (gate_score, rank_score)——命中任意一个组成部分就算相关。

    gate_score 恒为 precision，用于跟绝对及格线 _MIN_SCORE 比较；rank_score
    用于排序，实体召回传 use_fbeta=True。两者必须分开：F-beta 的 recall 项
    带 len(query)，拿它当绝对及格线会让实体随 query 变长而整个消失
    （见 fbeta_match_score 的说明）。

    字符集预过滤：候选名和 query 没有任何公共字符时，_matched_chars 必然
    返回 0，可以直接跳过后面 O(|query|*|name|) 的 DP。这个判据对当前的
    子序列打分是严格成立的（匹配上一个字符就至少需要一个公共字符），不像
    之前的 bigram 预过滤——那是给"最长连续公共子串+最小重叠2字符"设计的，
    对间隔约束子序列并不成立，实测 "订购单据编号是多少" 对 "订单号" 的
    bigram 交集为空却有 precision 1.0，会被错误跳过。
    """
    best_gate = best_rank = 0.0
    for name in names:
        if not name or set(name.lower()).isdisjoint(query_chars):
            continue
        matched = _matched_chars(query_text, name)
        if not matched:
            continue
        gate = matched / len(name)
        if use_fbeta:
            recall = matched / len(query_text)
            b2 = _FBETA * _FBETA
            rank = (1 + b2) * gate * recall / (b2 * gate + recall)
        else:
            rank = gate
        best_gate = max(best_gate, gate)
        best_rank = max(best_rank, rank)
    return best_gate, best_rank


def _rank(
    scored: list[tuple[float, float, object]], *, top_k: int, min_score: float = _MIN_SCORE
) -> list[object]:
    """scored 是 (gate_score, rank_score, payload) 三元组：按 gate_score 过
    及格线，按 rank_score 排序。实体召回传更高的 min_score，见
    _ENTITY_MIN_SCORE 的说明。"""
    kept = [(rank, payload) for gate, rank, payload in scored if gate >= min_score]
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
    # 字符集预过滤的可靠性说明见 _best_scores 的 docstring。
    query_chars = set(query_text.lower())

    term_types = _rank(
        [
            (*_best_scores(query_text, query_chars, name), name)
            for name in term_type_schema
        ],
        top_k=_TERM_TYPE_TOP_K,
    )
    relations = _rank(
        [
            (
                *_best_scores(
                    query_text, query_chars,
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
            (*_best_scores(query_text, query_chars, field.name), (term_type, field.name))
            for term_type, category in term_type_schema.items()
            for field in category.extra_fields
        ],
        top_k=_FIELD_TOP_K,
    )
    entities = _rank(
        [
            (
                *_best_scores(
                    query_text, query_chars, term.standard_name, use_fbeta=True,
                ),
                term,
            )
            for term in terms
        ],
        top_k=_ENTITY_TOP_K,
        min_score=_ENTITY_MIN_SCORE,
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
        # 查询（比如"这个类目下的产品分别属于哪些公司"要经过"产品"这个中间
        # 类型），最容易让深层参数生成 LLM 自己漏掉中间那一跳——这里把完整
        # 路径拼好递过去，不要求它自己从上面摊平的单跳关系列表里推理拼接。
        #
        # 注意这里给出的多跳路径【不适合直接用来计数】：中间类型和终点类型
        # 之间只要是多对多，沿路径计数就会把归属放大（2026-08-29 实测："公司
        # 有多少个订单"走 订单号→产品→公司，而每个产品都被 3 家公司卖过，
        # 三家公司的计数全都等于订单总数 10000，真实值是 3353/3330/3317）。
        # 这条注释原本就是拿"公司有多少个订单"当正面示例的，那是个反面例子，
        # 已换掉。计数场景的正确引导在 tool.py 的 _USAGE_GUIDE 里（优先找直连
        # 一跳，多跳只是退路且结果要标注成推导值），检测机制见
        # docs/superpowers/specs/2026-08-29-fan-trap-detection-design.md。
        # 箭头方向跟
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
