"""写入关系边这一个能力的协议。

三条写入路径各自声明了一个只暴露自己所需方法的窄协议（2026-08-27 架构评审
对 GraphClientProtocol 过宽问题的处理）：

- app/graphrag/normalization.py::GraphWriteClientProtocol —— 摄取管道
- app/graphrag/review_queue.py::ReviewGraphClientProtocol —— 人工审核批准
- app/graphrag/schema_etl.py::SchemaEtlGraphProtocol —— 结构化 ETL

窄协议本身是对的：这三个是不同的消费方，谁也不该看见别人的方法。但
merge_relation 的完整签名在三处被逐字抄了三遍，任何一次参数改动都要手工
同步三份，而这个项目没有类型检查（pyproject.toml 里没有 mypy/pyright，
CI 只跑 pytest），三份抄歪了不会有任何信号。

所以签名收进这里，三个窄协议继承它再各自补自己的方法——消费方可见的方法
集合一个不变，签名只写一遍。这不是把三个协议合并成一个；
tests/graphrag/test_relation_writer_protocol.py 有一条用例专门盯着别让它
被合并。

这三条路径也正是 app/graphrag/provenance.py 里三个取值对应的路径。
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class RelationWriterProtocol(Protocol):
    """能往图谱里写一条关系边。

    subject_standard_name/object_standard_name 这两个参数名是历史遗留，
    传进来的值必须是 node_key（创建时固定的身份键，改名后不变——ADR-0003），
    不是当前展示名——详见 neo4j_client.py::merge_relation 的完整说明。
    """

    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
        source: str,
        tenant_id: str,
        provenance: str,
        recorded_at: datetime,
    ) -> None: ...
