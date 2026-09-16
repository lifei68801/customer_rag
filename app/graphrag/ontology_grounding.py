"""落地状态：某个实体类型/关系类型有没有真实数据支撑。

**纯推导，不存、不缓存**（spec 决策 7）：ETL 映射里有一条指向它就是落地，
否则未落地。存一份的话就有两个真相——用户在表格导入页改了映射，工作台里
那份标记不会跟着变，而没有任何东西告诉他两者已经不一致。

草稿映射优先于已确认映射：用户刚在工作台点了"应用"，写下去的是草稿映射；
这时若仍按已确认的算，他会看到自己刚接上的表显示"未落地"。
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
import yaml

from app.graphrag.ontology_etl_mapping import get_etl_mapping
from app.graphrag.schema_etl_config import (
    InvalidSchemaETLConfigError,
    parse_schema_etl_config,
)


@dataclass(frozen=True)
class Grounding:
    #: 这份落地状态是从哪个版本的映射算出来的：draft / confirmed / None（没有映射）
    status: str | None
    grounded_term_types: list[str]
    grounded_relation_types: list[str]
    #: 映射里出现过的表名，去重排序。界面上用来回答"这些结论是看哪几张表得出的"。
    source_files: list[str]
    #: 映射存在但解析失败时的原因。此时三个列表都是空的——**不是**"没有落地"，
    #: 而是"算不出来"，界面必须把这句话显示出来，否则用户会以为数据全掉了。
    parse_error: str | None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "grounded_term_types": self.grounded_term_types,
            "grounded_relation_types": self.grounded_relation_types,
            "source_files": self.source_files,
            "parse_error": self.parse_error,
        }


async def derive_grounding(conn: aiosqlite.Connection, tenant_id: str) -> Grounding:
    for status in ("draft", "confirmed"):
        mapping = await get_etl_mapping(conn, tenant_id, status=status)
        if mapping is None:
            continue
        try:
            config = parse_schema_etl_config(
                mapping.config_yaml, origin=f"{tenant_id} 的 {status} ETL 映射"
            )
        except (InvalidSchemaETLConfigError, yaml.YAMLError) as exc:
            return Grounding(
                status=status,
                grounded_term_types=[],
                grounded_relation_types=[],
                source_files=[],
                parse_error=str(exc),
            )
        return Grounding(
            status=status,
            grounded_term_types=sorted({e.term_type for e in config.entities}),
            grounded_relation_types=sorted({r.relation_type for r in config.relations}),
            source_files=sorted(
                {e.source_file for e in config.entities} | {r.source_file for r in config.relations}
            ),
            parse_error=None,
        )
    return Grounding(
        status=None,
        grounded_term_types=[],
        grounded_relation_types=[],
        source_files=[],
        parse_error=None,
    )
