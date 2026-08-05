from __future__ import annotations

from typing import Any, Protocol

_SUBGRAPH_QUERY = """
MATCH (t:Term {standard_name: $standard_name})-[r]-(related:Term)
RETURN related.standard_name AS related_name, type(r) AS relation_type
"""

# Cypher 关系类型不能参数化，必须是查询字符串里的字面量；
# 用白名单杜绝把未经校验的 LLM 抽取结果拼进 Cypher 语句。
_ALLOWED_RELATION_TYPES = frozenset({"RELATED_TO", "BELONGS_TO_MODULE", "ALIAS_OF"})


class Neo4jSessionProtocol(Protocol):
    async def run(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> Any: ...

    async def __aenter__(self) -> "Neo4jSessionProtocol": ...

    async def __aexit__(self, *args: object) -> None: ...


class Neo4jDriverProtocol(Protocol):
    def session(self) -> Neo4jSessionProtocol: ...


class Neo4jGraphClient:
    """Neo4j 图查询封装：给定标准术语名，返回与之相关的子图。

    别名到标准名的归一化在应用层（term_matcher）完成，这里只处理
    已归一化的标准名查询，保证返回给 LLM 的上下文使用统一的标准
    名称，而不是原样带入各种不同表述。
    """

    def __init__(self, *, driver: Neo4jDriverProtocol) -> None:
        self._driver = driver

    async def query_subgraph(self, standard_name: str) -> list[dict[str, Any]]:
        async with self._driver.session() as session:
            result = await session.run(
                _SUBGRAPH_QUERY, {"standard_name": standard_name}
            )
            return await result.data()

    async def merge_relation(
        self,
        *,
        subject_standard_name: str,
        object_standard_name: str,
        relation_type: str,
    ) -> None:
        """幂等写入一条术语间关系（MERGE，不存在则创建，存在则不重复）。"""
        if relation_type not in _ALLOWED_RELATION_TYPES:
            raise ValueError(
                f"不允许的关系类型: {relation_type!r}，"
                f"仅支持: {sorted(_ALLOWED_RELATION_TYPES)}"
            )
        query = (
            "MERGE (a:Term {standard_name: $subject_name}) "
            "MERGE (b:Term {standard_name: $object_name}) "
            f"MERGE (a)-[:{relation_type}]->(b)"
        )
        async with self._driver.session() as session:
            await session.run(
                query,
                {
                    "subject_name": subject_standard_name,
                    "object_name": object_standard_name,
                },
            )
