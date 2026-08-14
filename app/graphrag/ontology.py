from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Term:
    standard_name: str
    aliases: list[str]
    term_type: str
    product_line: str
    extra_properties: dict[str, str] = field(default_factory=dict)


def load_terminology(path: Path) -> list[Term]:
    """加载人工维护的术语表（标准名称+别名+类型+产品线）。

    这是第4节设计的"基准真相"：LLM 抽取的实体必须向这份表对齐，
    而不是反过来。真实内容需由业务/技术支持团队协作产出，本函数
    只负责解析格式，不提供任何示例数据本身。
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    terms = data.get("terms", []) if isinstance(data, dict) else []
    return [
        Term(
            standard_name=str(item["standard_name"]),
            aliases=[str(a) for a in item.get("aliases", [])],
            term_type=str(item.get("term_type", "")),
            product_line=str(item.get("product_line", "")),
        )
        for item in terms
    ]
