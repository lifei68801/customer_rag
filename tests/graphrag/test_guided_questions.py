import asyncio

import aiosqlite

from app.graphrag.guided_questions import generate_questions
from app.graphrag.ontology_categories import create_term_type
from app.graphrag.ontology_constraints import add_allowed_combination
from app.graphrag.ontology_lifecycle import checkout_draft, confirm_ontology, ensure_ontology_schema


class FakeGraph:
    """按 (relation_type, from, to) 报告边数。没登记的组合返回 0——
    「图里没有这种边」正是这个类要模拟的那个状态。"""

    def __init__(self, fanouts: dict[tuple[str, str, str], int]) -> None:
        self._fanouts = fanouts
        self.probes: list[tuple[str, str, str]] = []

    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int:
        key = (relation_type, from_term_type, to_term_type)
        self.probes.append(key)
        return self._fanouts.get(key, 0)


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await ensure_ontology_schema(conn)
    return conn


async def _add_combinations(
    conn: aiosqlite.Connection,
    tenant_id: str,
    combos: list[tuple[str, str, str]],
    *,
    actor: str = "alice",
    confirm: bool = True,
) -> None:
    """把一批 (subject_term_type, relation_type, object_term_type) 从零建到
    draft（可选再 confirm）状态，按仓库权威写法
    （tests/api/test_admin_document_routes.py:112-123）：

    checkout_draft -> create_term_type（每个 term_type 各一次）->
    add_allowed_combination -> 可选 confirm_ontology。

    add_allowed_combination 没有 status 参数，恒定插入 draft
    （app/graphrag/ontology_constraints.py:120-149），要让组合出现在
    confirmed 列表里必须显式调用 confirm_ontology；「只调用到 add，不调用
    confirm」正是 test_only_confirmed_combinations_are_used 需要的状态。

    checkout_draft 对全新租户（默认 ingestion_mode='extraction'，见
    app/graphrag/tenant_ingestion_config.py:24-32）会播种 10 种通用关系类型
    （app/graphrag/ontology_relations.py:39-50，含 RELATED_TO），本文件的
    组合全部复用被播种的 RELATED_TO，不额外注册新关系类型
    ——add_allowed_combination 内部会校验关系类型已在该租户 draft 中登记
    （ontology_constraints.py:86-105 的 _validate_references）。
    """
    await checkout_draft(conn, tenant_id)
    created_term_types: set[str] = set()
    for subject_term_type, relation_type, object_term_type in combos:
        for term_type in (subject_term_type, object_term_type):
            if term_type not in created_term_types:
                await create_term_type(conn, tenant_id, value=term_type, actor=actor)
                created_term_types.add(term_type)
        await add_allowed_combination(
            conn, tenant_id, subject_term_type=subject_term_type,
            relation_type=relation_type, object_term_type=object_term_type, actor=actor,
        )
    if confirm:
        await confirm_ontology(conn, tenant_id, actor=actor)


def test_generates_a_question_per_combination_that_actually_has_edges():
    async def run():
        conn = await _conn()
        try:
            await _add_combinations(
                conn, "t1", [("产品", "RELATED_TO", "口味")],
            )
            graph = FakeGraph({("RELATED_TO", "产品", "口味"): 12})
            questions = await generate_questions(conn, graph, tenant_id="t1")
            assert questions == ["产品有哪些口味？"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_skips_combinations_with_no_edges_in_the_graph():
    """本体里声明了、图里一条数据都没有的组合不能生成问题。

    这是这个模块存在的全部理由：只看本体的话，一个刚建好还没导数据的租户
    会推荐一整屏答不出来的问题——用户点了产品自己推荐的问题却什么也没有，
    那是自伤。

    批次里同时有「有边」和「没边」两种：全都有边的话，「不过滤」的实现
    也能变绿。
    """

    async def run():
        conn = await _conn()
        try:
            await _add_combinations(
                conn, "t1",
                [("产品", "RELATED_TO", "口味"), ("产品", "RELATED_TO", "产地")],
            )
            graph = FakeGraph({("RELATED_TO", "产品", "口味"): 12})  # 产地组合没边
            questions = await generate_questions(conn, graph, tenant_id="t1")
            assert questions == ["产品有哪些口味？"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_only_confirmed_combinations_are_used():
    """草稿态的本体不该出现在前台。草稿是还没定的东西，拿它生成问题
    等于把内部草稿念给终端用户听。"""

    async def run():
        conn = await _conn()
        try:
            await _add_combinations(
                conn, "t1", [("产品", "RELATED_TO", "口味")], confirm=False,
            )
            graph = FakeGraph({("RELATED_TO", "产品", "口味"): 12})
            assert await generate_questions(conn, graph, tenant_id="t1") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_other_tenants_combinations_are_not_used():
    async def run():
        conn = await _conn()
        try:
            await _add_combinations(
                conn, "t2", [("产品", "RELATED_TO", "口味")],
            )
            graph = FakeGraph({("RELATED_TO", "产品", "口味"): 12})
            assert await generate_questions(conn, graph, tenant_id="t1") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_result_is_capped_and_the_busiest_combinations_win():
    """上限 4 条。超出时留边最多的那几个——它们最可能真的有内容可答。

    四个组合边数各不相同（1/40/7/3），断言拿到的是边最多的三个且顺序正确。
    边数相同的话，「按边数排」和「按字典序排」两种实现都能变绿。
    """

    async def run():
        conn = await _conn()
        try:
            combos = [
                ("A", "RELATED_TO", "B"), ("C", "RELATED_TO", "D"),
                ("E", "RELATED_TO", "F"), ("G", "RELATED_TO", "H"),
            ]
            await _add_combinations(conn, "t1", combos)
            graph = FakeGraph({
                ("RELATED_TO", "A", "B"): 1, ("RELATED_TO", "C", "D"): 40,
                ("RELATED_TO", "E", "F"): 7, ("RELATED_TO", "G", "H"): 3,
            })
            questions = await generate_questions(conn, graph, tenant_id="t1", limit=3)
            assert questions == ["C有哪些D？", "E有哪些F？", "G有哪些H？"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_a_graph_failure_yields_no_questions_rather_than_unverified_ones():
    """图谱查不通时返回空列表，不是「跳过校验、把本体里的组合都生成出来」。

    降级成不校验的话，恰恰在最可能出问题的时刻（图谱不可用）给出一屏
    保证答不出来的问题。空的引导区是诚实的，坏的引导区不是。

    批次里故意放两个组合：一个探测会抛异常（PART_OF），另一个探测本来
    能拿到边数（RELATED_TO，fanout=12）。只放一个会抛异常的组合不够——
    那样的话「查不通就整体中止、返回 []」和「查不通就跳过这一个、继续
    生成别的」这两种实现在只有一条数据时结果碰巧一样（都是空列表），
    区分不出「中止」和「跳过继续」。批次里必须还有一个「探测本可以成功」
    的组合，才能验证前者会把这个本可以成功的结果也一起丢弃，而后者不会。
    """

    class PartlyBrokenGraph:
        async def probe_relation_fanout(
            self, *, relation_type: str, **_: object
        ) -> int:
            if relation_type == "PART_OF":
                raise RuntimeError("Neo4j 连不上")
            return 12

    async def run():
        conn = await _conn()
        try:
            await _add_combinations(
                conn, "t1",
                [("产品", "RELATED_TO", "口味"), ("产品", "PART_OF", "产地")],
            )
            questions = await generate_questions(conn, PartlyBrokenGraph(), tenant_id="t1")
            assert questions == []
        finally:
            await conn.close()

    asyncio.run(run())
