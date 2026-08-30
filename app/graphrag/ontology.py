from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Term:
    tenant_id: str
    node_key: str
    standard_name: str
    aliases: list[str]
    term_type: str
    extra_properties: dict[str, str | int | float | list[float]] = field(default_factory=dict)
    source: str = "unknown"


def _name_matches(name: str, term: Term) -> bool:
    """候选名是否字面命中这个 Term 的 standard_name 或某个 alias——大小写
    不敏感。

    2026-08-29 补的大小写归一。在那之前这里是纯 == 比较，"coca-cola" 命
    中不了 "Coca-Cola"，"LATTE" 命中不了别名 "Latte"。项目里其余所有字符
    匹配环节（term_matcher.matched_length、ontology_recall 的
    precision/F-beta 打分、term_guard 的模糊层）早就统一在比较前转小写了，
    只有这条"精确匹配"路径没跟上，导致同一个名字在召回阶段能匹配上、到了
    解析阶段反而解析不出来。

    structured_filter_query 的约束值路径有模糊兜底
    （_best_fuzzy_term_name）会掩盖这个差异，但 anchor.name 路径、
    normalization.py 的 ETL 归一化、review_queue.py 的人工审核批准都直接
    依赖这里的返回值，大小写不一致在那几条路径上是硬失败。

    归一化后如果两条不同的 Term 撞名（比如 "Cola" 和 "COLA"），命中数会从
    1 变成 2，按既有的"唯一一条才算解析成功"策略返回 None——这是有意的，
    不会因为归一化就放松消歧策略、从多条里随便选一条。
    """
    lowered = name.lower()
    return lowered == term.standard_name.lower() or any(
        lowered == alias.lower() for alias in term.aliases
    )


def resolve_term(
    name: str, terms: list[Term], *, term_type_hint: str | None = None
) -> Term | None:
    """按候选名（可以是某个术语的 standard_name 本身，也可以是它的某个
    alias）在 terms 里找唯一对应的 Term——是全部"按名字查 Term"调用路径
    统一使用的消歧逻辑（LLM 抽取归一化 normalization.py、人工审核批准
    review_queue.py、RAG 检索工具 agent/tools.py）。

    2026-08-23 之前这三处各自调用两个不同的函数：这个函数的前身
    find_term_by_type_hint 只按 standard_name 字段去重（忽略 aliases）；
    normalization.py 内部私有的 _resolve_term 按 name-or-alias 去重。两套
    判重规则在"候选名通过别名唯一命中某个 Term，但这个 Term 的
    standard_name 恰好和另一个不相关、不同类型的 Term 撞名"时会给出不同
    答案——这正是 normalize_and_write_relations 里 2026-08-22 Fix round 1
    调查记录的那类 bug 的根源，也是 graph_query_tool 当初为了绕开同一个坑、
    改成直接导入 normalization.py 私有函数（而不是这个模块本来该提供的公开
    函数）的原因（`graph_query_tool` 已在 2026-08-24 的收尾任务中并入
    `structured_filter_query_tool`，历史脉络见 docs/AGENT_PLANNER_DESIGN.md
    §4.1，此处仅保留发现问题时的原始上下文）。本函数把三处调用方收敛到
    同一个实现、同一套判重规则，
    从根上消除"策略分叉"这件事本身，而不是让每个调用方各自小心。

    term_type_hint 传了且该类型下按 name-or-alias 恰好命中一条：直接返回
    那一条——即使 hint 本身可能不完全准确（比如上游只是"猜测"的类型
    候选，不是强校验过的值），只要该类型下确实唯一命中这个名字/别名，
    就认为这是调用方想要的那一条。该类型下命中两条及以上（比如术语表
    本身在同一 term_type 内出现了别名冲突——见 terms_store.py 的
    upsert_term_with_node_key，ETL 写入路径不会像 create_term/update_term
    那样做别名冲突检查）：同样返回 None，不会因为"传了 hint"就放松
    "唯一一条才算解析成功"这条策略、从多条里随便选第一条命中的。

    没有精确命中该类型（没传 hint，或者传了但该类型下没有任何匹配）：
    退回"候选名作为 standard_name 或某个 alias，在全部术语里一共匹配
    几条"——只匹配一条就直接返回它（没有歧义的安全情况，覆盖绝大多数
    "名字本身不重复"的调用），匹配零条或两条以上都返回 None，交给调用方
    走各自已有的"未找到/不明确"错误分支，而不是从多个候选里随便选一个。
    找不到和有歧义这两种"返回 None"的情况如果需要进一步区分（比如给
    人看的错误提示），用 find_candidate_term_types 单独判断。
    """
    resolved = resolve_term_or_candidates(name, terms, term_type_hint=term_type_hint)
    return resolved if isinstance(resolved, Term) else None


def resolve_term_or_candidates(
    name: str, terms: list[Term], *, term_type_hint: str | None = None
) -> Term | list[Term]:
    """按候选名解析 Term，把"没找到"和"有歧义"区分开。

    唯一命中返回那个 Term；命中多条返回全部候选（长度 ≥2）；零命中返回空
    列表。resolve_term 是它的薄封装，把"多候选"和"零命中"都压成 None——
    那个语义对既有调用方仍然正确，不改。

    这个函数存在的理由：standard_name 的唯一索引取消之后，同一 term_type
    下同名成为合法状态，歧义会真实出现。调用方如果只能拿到 None，就无法
    向用户澄清"你说的是哪一个"，只能报"没找到"——而那是错的。
    """
    if term_type_hint:
        hinted = [
            t for t in terms
            if t.term_type == term_type_hint and _name_matches(name, t)
        ]
        if len(hinted) == 1:
            return hinted[0]
        if len(hinted) > 1:
            return hinted
    matches = [t for t in terms if _name_matches(name, t)]
    if len(matches) == 1:
        return matches[0]
    return matches


def find_candidate_term_types(name: str, terms: list[Term]) -> list[str]:
    """候选名（standard_name 或某个 alias）在 terms 里实际匹配到的
    term_type 集合，按字母序去重排列——不做消歧，只回答"这个名字到底
    存不存在、如果存在都分布在哪些类型下"。

    供 resolve_term 返回 None 时的调用方区分"根本找不到"（返回空列表）
    和"找到了但有歧义、需要更明确的类型提示才能确定唯一一条"（返回两个
    及以上的 term_type）——例如 review_queue.py::approve_review 用这个
    区分来生成更准确的错误提示，而不是笼统地说"不在术语表中"。
    """
    return sorted({
        t.term_type for t in terms if _name_matches(name, t)
    })


def load_terminology(path: Path) -> list[Term]:
    """加载人工维护的术语表（标准名称+别名+类型）。

    这是第4节设计的"基准真相"：LLM 抽取的实体必须向这份表对齐，
    而不是反过来。真实内容需由业务/技术支持团队协作产出，本函数
    只负责解析格式，不提供任何示例数据本身。

    这份 YAML 只在术语表首次建表时一次性导入（见 terms_store.py::
    ensure_terms_schema），导入目标固定是 tenant_id="default"——YAML
    种子文件本身不区分租户，是单租户部署时代遗留的初始化路径。
    node_key 在导入时直接取 standard_name 的值（Global Constraints 的
    node_key 生成规则）。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    terms = data.get("terms", []) if isinstance(data, dict) else []
    return [
        Term(
            tenant_id="default",
            node_key=str(item["standard_name"]),
            standard_name=str(item["standard_name"]),
            aliases=[str(a) for a in item.get("aliases", [])],
            term_type=str(item.get("term_type", "")),
        )
        for item in terms
    ]
