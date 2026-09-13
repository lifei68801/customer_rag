from __future__ import annotations

import json

from app.graphrag.ontology import Term
from app.graphrag.ontology_recall import longest_common_substring_score

_DUPLICATE_SIMILARITY_THRESHOLD = 0.6


def _names_of(term: Term) -> list[str]:
    return [term.standard_name, *term.aliases]


def _is_purely_numeric(name: str) -> bool:
    """纯数字标准名（如批量导入的"销量"术语 100/101/102...）两两之间按
    LCS/较短字符串长度打分极易超阈值（"100"跟"101"共享"10"两个字符，
    2/3≈0.67），会把审核队列刷爆成大量毫无意义的建议——这类术语直接跳过
    检测，不参与相似度比对（无论作为批跑的两两比对候选，还是创建时点
    提示的候选/已有术语）。"""
    return name.strip().isdigit()


def term_similarity_score(a: Term, b: Term) -> float:
    """两条术语的相似度——比对范围是各自的 standard_name + 全部 aliases，
    两两取最高分；longest_common_substring_score 本身对 a/b 不对称
    （除以 len(b)），这里取两个方向的最大值。"""
    names_a = _names_of(a)
    names_b = _names_of(b)
    return max(
        (
            max(
                longest_common_substring_score(name_a, name_b),
                longest_common_substring_score(name_b, name_a),
            )
            for name_a in names_a
            for name_b in names_b
        ),
        default=0.0,
    )


def find_similar_terms(
    candidate_name: str, existing_terms: list[Term]
) -> list[tuple[Term, float]]:
    """candidate_name 是一个裸字符串（尚未创建成 Term），跟 existing_terms
    里每一条的 standard_name/aliases 比对，返回超过阈值的 (Term, score)，
    按 score 降序排列。"""
    if _is_purely_numeric(candidate_name):
        return []
    scored = []
    for term in existing_terms:
        if _is_purely_numeric(term.standard_name):
            continue
        score = max(
            (
                max(
                    longest_common_substring_score(candidate_name, name),
                    longest_common_substring_score(name, candidate_name),
                )
                for name in _names_of(term)
            ),
            default=0.0,
        )
        if score >= _DUPLICATE_SIMILARITY_THRESHOLD:
            scored.append((term, score))
    scored.sort(key=lambda item: (-item[1], item[0].standard_name))
    return scored


#: 一个字段算"像标识"的门槛：同类型下它的非空值至少这么大比例互不相同。
#:
#: 不取 0.95 这种更"严格"的值：真重复本身会拉低这个比例——两条重复记录
#: 共用同一个手机号。一个 100 人的类型里有 10 对重复，比例就是 0.90；门槛
#: 定在 0.95 的话，手机号恰恰在重复最多、最需要反证的数据上被判成"不像
#: 标识"。价格、等级、类别这类字段的比例远低于 0.9，不会被误认。
_IDENTIFIER_DISTINCT_RATIO = 0.9
#: 至少要有这么多个非空值才判——三条数据里的三个值当然互不相同，但那说明
#: 不了这个字段是标识。
_IDENTIFIER_MIN_VALUES = 5


def _normalized(value: object) -> str | None:
    """用于比较的规整形式。空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        # 42.0 和 42 是同一个值。不收的话同一个编码因为一边是 ETL 读出的
        # float、一边是界面填的 int，被判成"冲突"。
        value = int(value)
    if isinstance(value, (list, tuple)):
        return json.dumps([_normalized(v) for v in value], ensure_ascii=False)
    text = str(value).strip().lower()
    return text or None


def identifier_like_fields(terms: list[Term]) -> set[str]:
    """同一批（同类型）实体里，哪些属性字段的值几乎两两不同——像手机号、
    编码、证件号。

    **从数据里算，不靠配置。** 本体 schema 里没有"这个字段唯一"的声明，
    加一个又得让建模的人多判断一次；而这件事数据本身就回答得了：价格、
    类别这种字段大量重复，编码、手机号这种几乎不重复。
    """
    values: dict[str, list[str]] = {}
    for term in terms:
        for field, raw in (term.extra_properties or {}).items():
            normalized = _normalized(raw)
            if normalized is not None:
                values.setdefault(field, []).append(normalized)
    return {
        field
        for field, vals in values.items()
        if len(vals) >= _IDENTIFIER_MIN_VALUES
        and len(set(vals)) / len(vals) >= _IDENTIFIER_DISTINCT_RATIO
    }


def has_identifier_conflict(a: Term, b: Term, identifier_fields: set[str]) -> bool:
    """两条实体在某个像标识的字段上都有值、且值不同。

    **只看像标识的字段，不看全部字段。** 来自不同数据源的真重复，属性值
    经常就是不一样的——A 表的价格是旧的、B 表是新的——那正是属性冲突队列
    存在的原因。"任意字段不同就不是重复"会把真重复也压掉。

    一边有值一边没有不算冲突：缺失不是反证，只是信息不全。
    """
    props_a = a.extra_properties or {}
    props_b = b.extra_properties or {}
    for field in identifier_fields:
        va = _normalized(props_a.get(field))
        vb = _normalized(props_b.get(field))
        if va is not None and vb is not None and va != vb:
            return True
    return False


def find_duplicate_pairs(terms: list[Term]) -> list[tuple[Term, Term, float]]:
    """对 terms 两两比对，返回超过阈值的 (term_a, term_b, score)——term_a/
    term_b 按 node_key 字符串排序，保证同一对术语的结果跟输入列表顺序无关。
    调用方负责先按 term_type 分组再传进来，这个函数本身不做分组。

    名字像、但在像标识的字段上值不同的，**不推**。两个都叫「张伟」的客户
    名字打分 1.0，但手机号不同，是两个人。不挡住的话同名不同人会把疑似重复
    队列刷满，而审核员一旦发现队列里大半是误报，就会开始不看它——真重复也
    跟着没人看了。
    """
    terms = [t for t in terms if not _is_purely_numeric(t.standard_name)]
    identifier_fields = identifier_like_fields(terms)
    pairs: list[tuple[Term, Term, float]] = []
    for i in range(len(terms)):
        for j in range(i + 1, len(terms)):
            score = term_similarity_score(terms[i], terms[j])
            if score >= _DUPLICATE_SIMILARITY_THRESHOLD and not has_identifier_conflict(
                terms[i], terms[j], identifier_fields
            ):
                term_a, term_b = sorted([terms[i], terms[j]], key=lambda t: t.node_key)
                pairs.append((term_a, term_b, score))
    return pairs
