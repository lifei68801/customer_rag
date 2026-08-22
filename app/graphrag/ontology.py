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


def find_term_by_type_hint(
    terms: list[Term], standard_name: str, term_type_hint: str | None = None
) -> Term | None:
    """按 standard_name 在 terms 里找唯一对应的 Term，尽量避免"多个同名不同
    类型的术语存在时静默选错"。

    term_type_hint 传了且该类型下存在这个 standard_name：直接返回那一条，
    不再考虑其它同名术语——即使 hint 本身可能不完全准确（比如上游只是
    "猜测"的类型候选，不是强校验过的值），只要该类型下确实有这个名字，
    就认为这是调用方想要的那一条。

    没有精确命中该类型（没传 hint，或者传了但该类型下没有这个名字）：
    退回"这个 standard_name 在全部术语里一共出现几次"——只出现一次就
    直接返回它（没有歧义的安全情况，覆盖绝大多数"名字本身不重复"的
    调用），出现零次或两次以上都返回 None，交给调用方走各自已有的
    "未找到/不明确"错误分支，而不是从多个候选里随便选一个。

    2026-08-22 起 standard_name 允许跨 term_type 重复（同一租户下不同
    类型可以共享同一个显示名，见 terms_store.py 里
    idx_terms_tenant_standard_name 的新定义），这个函数是所有"按名字查
    Term"调用方（LLM 抽取归一化、人工审核批准、RAG 检索工具）统一使用
    的消歧逻辑，避免各自重复实现、行为不一致。
    """
    if term_type_hint:
        for term in terms:
            if term.term_type == term_type_hint and term.standard_name == standard_name:
                return term
    same_name = [t for t in terms if t.standard_name == standard_name]
    if len(same_name) == 1:
        return same_name[0]
    return None


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
