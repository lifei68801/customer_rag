# 引导问题 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前台空会话时给出 3–6 条引导问题，每一条**点了都能答出来**。

**Architecture:** 两档来源（spec D2）：后台手写优先，一条都没配时从本体自动兜底。两档共用同一条硬要求——手写那批保存时真跑一遍实体匹配，自动那批只用图里真有边的类型组合（`probe_relation_fanout` 返回 > 0）。失效的手写问题不静默消失，在看板上变成待办。

**Tech Stack:** FastAPI · aiosqlite · Neo4j · React 18 + TypeScript · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-08-multi-persona-foundation.md`（需要 `tenant_personas` 表的 `questions` 列与 `GET /api/admin/personas`）

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的 Global Constraints 十二条，逐字适用。额外一条：

13. **引导问题的两条硬规矩不可协商**：点了必须能答出来；失效了必须有人知道。任何为了让实现简单而放宽这两条的改动，都要先回到 spec D2 重新论证。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/graphrag/guided_questions.py`（新） | 从本体自动生成引导问题；判断一条问题是否还命中 |
| `app/graphrag/tenant_personas_store.py`（改） | 加 `set_questions` / `get_questions` |
| `app/api/admin_personas_routes.py`（改） | 回包里带上 `questions`；新增写入端点 + 校验端点 |
| `frontend/src/lib/personasApi.ts`（改） | `Persona` 加 `questions: string[]` |
| `frontend/src/components/GuidedQuestions.tsx`（新） | 前台空会话时的问题卡片 |
| `frontend/src/pages/ChatPage.tsx`（改） | 空会话时渲染它 |
| `frontend/src/admin/PersonaEditorPage.tsx`（新） | 后台配置头像 / 人设 / 引导问题 |

---

## Task 1: 从本体自动生成引导问题

**Files:**
- Create: `app/graphrag/guided_questions.py`
- Test: `tests/graphrag/test_guided_questions.py`

**Interfaces:**
- Consumes:
  - `app.graphrag.ontology_constraints.list_allowed_combinations(conn, tenant_id, *, status) -> list[AllowedCombination]`（字段：`subject_term_type` / `relation_type` / `object_term_type`）
  - `GraphWriteProtocol.probe_relation_fanout(*, tenant_id, relation_type, from_term_type, to_term_type, direction) -> int`
- Produces:
  - `async def generate_questions(conn, graph_client, *, tenant_id: str, limit: int = 4) -> list[str]`

- [ ] **Step 1: 写失败测试**

创建 `tests/graphrag/test_guided_questions.py`：

```python
import asyncio

import aiosqlite

from app.graphrag.guided_questions import generate_questions
from app.graphrag.ontology_constraints import (
    add_allowed_combination,
    ensure_constraints_schema,
)


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
    await ensure_constraints_schema(conn)
    return conn


def test_generates_a_question_per_combination_that_actually_has_edges():
    async def run():
        conn = await _conn()
        try:
            await add_allowed_combination(
                conn, tenant_id="t1", subject_term_type="产品",
                relation_type="HAS_FLAVOR", object_term_type="口味", status="confirmed",
            )
            graph = FakeGraph({("HAS_FLAVOR", "产品", "口味"): 12})
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
            for subj, rel, obj in [
                ("产品", "HAS_FLAVOR", "口味"),
                ("产品", "MADE_IN", "产地"),
            ]:
                await add_allowed_combination(
                    conn, tenant_id="t1", subject_term_type=subj,
                    relation_type=rel, object_term_type=obj, status="confirmed",
                )
            graph = FakeGraph({("HAS_FLAVOR", "产品", "口味"): 12})  # MADE_IN 没边
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
            await add_allowed_combination(
                conn, tenant_id="t1", subject_term_type="产品",
                relation_type="HAS_FLAVOR", object_term_type="口味", status="draft",
            )
            graph = FakeGraph({("HAS_FLAVOR", "产品", "口味"): 12})
            assert await generate_questions(conn, graph, tenant_id="t1") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_other_tenants_combinations_are_not_used():
    async def run():
        conn = await _conn()
        try:
            await add_allowed_combination(
                conn, tenant_id="t2", subject_term_type="产品",
                relation_type="HAS_FLAVOR", object_term_type="口味", status="confirmed",
            )
            graph = FakeGraph({("HAS_FLAVOR", "产品", "口味"): 12})
            assert await generate_questions(conn, graph, tenant_id="t1") == []
        finally:
            await conn.close()

    asyncio.run(run())


def test_result_is_capped_and_the_busiest_combinations_win():
    """上限 4 条。超出时留边最多的那几个——它们最可能真的有内容可答。

    四个组合边数各不相同（1/2/3/4），断言拿到的是边最多的三个且顺序正确。
    边数相同的话，「按边数排」和「按字典序排」两种实现都能变绿。
    """

    async def run():
        conn = await _conn()
        try:
            combos = [("A", "R1", "B"), ("C", "R2", "D"), ("E", "R3", "F"), ("G", "R4", "H")]
            for subj, rel, obj in combos:
                await add_allowed_combination(
                    conn, tenant_id="t1", subject_term_type=subj,
                    relation_type=rel, object_term_type=obj, status="confirmed",
                )
            graph = FakeGraph({
                ("R1", "A", "B"): 1, ("R2", "C", "D"): 40,
                ("R3", "E", "F"): 7, ("R4", "G", "H"): 3,
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
    """

    class BrokenGraph:
        async def probe_relation_fanout(self, **_: object) -> int:
            raise RuntimeError("Neo4j 连不上")

    async def run():
        conn = await _conn()
        try:
            await add_allowed_combination(
                conn, tenant_id="t1", subject_term_type="产品",
                relation_type="HAS_FLAVOR", object_term_type="口味", status="confirmed",
            )
            assert await generate_questions(conn, BrokenGraph(), tenant_id="t1") == []
        finally:
            await conn.close()

    asyncio.run(run())
```

- [ ] **Step 2: （已核对）确认 `add_allowed_combination` 的关键字参数**

`app/graphrag/ontology_constraints.py` 里真实存在的是（已核对）：
`ensure_constraints_schema(conn)` · `list_allowed_combinations(conn, tenant_id, *, status)` ·
`add_allowed_combination(...)` · `remove_allowed_combination(...)`。上面的用例已按这几个名字写好。

只剩一件要读源码的事：`add_allowed_combination` 的关键字参数名
（`app/graphrag/ontology_constraints.py:120`）。它内部会调 `_validate_references`
校验类型和关系是否已登记——用例里的 `产品` / `口味` / `HAS_FLAVOR` 可能需要先登记。
读一遍那个函数，若确实需要，在 `_conn()` 里补上登记步骤。

**改测试去迁就既有函数，不要为了迁就测试去加新函数。**

- [ ] **Step 3: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_guided_questions.py -q -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.graphrag.guided_questions'`

- [ ] **Step 4: 写实现**

创建 `app/graphrag/guided_questions.py`：

```python
from __future__ import annotations

import logging
from typing import Protocol

import aiosqlite

from app.graphrag.ontology_constraints import list_allowed_combinations

logger = logging.getLogger(__name__)

#: 自动生成的问题最多几条。跟手写那一档同一个量级——引导区超过五六条就
#: 没人读了，而它占的是首屏最值钱的位置。
DEFAULT_QUESTION_LIMIT = 4


class FanoutProbe(Protocol):
    async def probe_relation_fanout(
        self, *, tenant_id: str, relation_type: str, from_term_type: str,
        to_term_type: str, direction: str,
    ) -> int: ...


async def generate_questions(
    conn: aiosqlite.Connection,
    graph_client: FanoutProbe,
    *,
    tenant_id: str,
    limit: int = DEFAULT_QUESTION_LIMIT,
) -> list[str]:
    """从已确认本体生成引导问题，**只用图里真有边的类型组合**。

    只看本体不看图的话，一个刚建好还没导数据的租户会推荐一整屏答不出来的
    问题——用户点了产品自己推荐的问题却什么也没有。这是自伤，也是这个函数
    要 probe 一遍图的全部理由（见 spec D2 的裁决）。

    只用 status='confirmed'：草稿是还没定的东西，把它念给终端用户听等于
    把内部草稿泄露出去。

    边最多的组合排前面：它们最可能真的有内容可答。

    图谱查不通时返回空列表，**不降级成「跳过校验」**——那会恰恰在最可能
    出问题的时刻给出一屏保证答不出来的问题。空的引导区是诚实的。
    """
    combinations = await list_allowed_combinations(conn, tenant_id, status="confirmed")
    scored: list[tuple[int, str]] = []
    for combo in combinations:
        try:
            fanout = await graph_client.probe_relation_fanout(
                tenant_id=tenant_id,
                relation_type=combo.relation_type,
                from_term_type=combo.subject_term_type,
                to_term_type=combo.object_term_type,
                direction="outgoing",
            )
        except Exception:
            logger.warning(
                "租户 %r 的引导问题生成中止：探测关系 %s(%s→%s) 的图谱查询失败。"
                "不降级成跳过校验——那会给出一屏保证答不出来的问题",
                tenant_id, combo.relation_type,
                combo.subject_term_type, combo.object_term_type,
                exc_info=True,
            )
            return []
        if fanout > 0:
            scored.append(
                (fanout, f"{combo.subject_term_type}有哪些{combo.object_term_type}？")
            )
    # 先按边数倒序、再按问题文本正序：边数相同时顺序必须是确定的，
    # 否则同一个租户每次刷新看到的引导问题顺序都不一样。
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [question for _, question in scored[:limit]]
```

- [ ] **Step 5: 跑测试确认它绿**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_guided_questions.py -q -p no:cacheprovider`
Expected: 6 passed

- [ ] **Step 6: 变异验证**

```bash
# 变异 A：去掉 `if fanout > 0` 判断，全部生成
#   预期红：test_skips_combinations_with_no_edges_in_the_graph
# 变异 B：status 从 "confirmed" 改成 "draft"
#   预期红：test_only_confirmed_combinations_are_used
# 变异 C：except 分支改成 continue（跳过这一个，继续生成别的）
#   预期红：test_a_graph_failure_yields_no_questions_rather_than_unverified_ones
# 变异 D：排序键去掉负号（边最少的排前面）
#   预期红：test_result_is_capped_and_the_busiest_combinations_win
```

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/guided_questions.py tests/graphrag/test_guided_questions.py
git commit -m "feat(chat): 引导问题从本体自动生成，只用图里真有边的组合"
```

---

## Task 2: 手写问题的存取与保存时校验

**Files:**
- Modify: `app/graphrag/tenant_personas_store.py`
- Create: `app/graphrag/question_validation.py`
- Test: `tests/graphrag/test_question_validation.py`

**Interfaces:**
- Consumes: `app.graphrag.ontology.resolve_term_or_candidates(name, terms, *, term_type_hint=None) -> Term | list[Term]`（`app/graphrag/ontology.py:89`）
- Produces:
  - `async def set_questions(conn, *, tenant_id: str, questions: list[str]) -> None`
  - `async def get_questions(conn, tenant_id: str) -> list[str]`
  - `def find_unmatched_questions(questions: list[str], terms: list[Term]) -> list[str]`——返回一条实体都匹配不上的那几条

**校验判据**：一条引导问题里至少要能匹配上**一个**已知实体名或实体类型名。
这不是完整的可答性证明（那要真跑一遍问答管线，太慢，且会在保存时烧 LLM 调用），
但它挡住了绝大多数「问了个本体里根本没有的东西」——比如
「库存多少？」在一个没有「库存」类型的本体里。

- [ ] **Step 1: 写失败测试**

创建 `tests/graphrag/test_question_validation.py`：

```python
from app.graphrag.ontology import Term
from app.graphrag.question_validation import find_unmatched_questions


def _term(name: str, term_type: str) -> Term:
    """Term 的字段已核对（app/graphrag/ontology.py:10-17）：
    tenant_id / node_key / standard_name / aliases / term_type /
    extra_properties（默认空 dict）/ source（默认 'unknown'）。"""
    return Term(
        tenant_id="t1",
        node_key=f"{term_type}:{name}",
        standard_name=name,
        aliases=[],
        term_type=term_type,
    )


def test_a_question_naming_a_known_entity_passes():
    terms = [_term("Beer", "产品"), _term("柚子", "口味")]
    assert find_unmatched_questions(["Beer 是什么口味的？"], terms) == []


def test_a_question_naming_a_known_type_passes():
    """问「产品有哪些口味」时，「产品」和「口味」都是类型名不是实体名。
    只认实体名的话，本体里最典型的那类问题会被判成不可答。"""
    terms = [_term("Beer", "产品"), _term("柚子", "口味")]
    assert find_unmatched_questions(["产品有哪些口味？"], terms) == []


def test_a_question_naming_nothing_in_the_ontology_is_reported():
    """「库存多少？」在一个没有库存概念的本体里必须被报出来。
    这是这个函数存在的理由。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(["库存多少？"], terms) == ["库存多少？"]


def test_only_the_unmatched_ones_are_returned():
    """批次里同时有能匹配和不能匹配的。全都能匹配的批次下，
    「一律返回空」的实现也能变绿。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(
        ["Beer 是什么？", "库存多少？", "产品有哪些？"], terms
    ) == ["库存多少？"]


def test_an_empty_ontology_reports_every_question():
    """本体是空的时候，任何手写问题都答不出来。此时全部报出来而不是
    全部放行——放行的话，新租户配的问题一条都点不动，而保存时什么都没说。"""
    assert find_unmatched_questions(["随便什么"], []) == ["随便什么"]


def test_matching_ignores_case_and_surrounding_punctuation():
    """「beer 是什么？」和「Beer 是什么？」是同一个问题。
    大小写敏感的匹配会让审核员反复困惑于为什么保存不了。"""
    terms = [_term("Beer", "产品")]
    assert find_unmatched_questions(["beer 是什么？"], terms) == []
```

- [ ] **Step 2: （已核对，无需探索）**

`Term` 的字段已在上面的 `_term` 里写死，来自 `app/graphrag/ontology.py:10-17`。
注意 `extra_properties` 和 `source` 有默认值，其余五个必填。

- [ ] **Step 3: 跑测试确认它红**

Run: `.venv/Scripts/python.exe -m pytest tests/graphrag/test_question_validation.py -q -p no:cacheprovider`

- [ ] **Step 4: 写实现**

创建 `app/graphrag/question_validation.py`：

```python
from __future__ import annotations

from app.graphrag.ontology import Term


def find_unmatched_questions(questions: list[str], terms: list[Term]) -> list[str]:
    """挑出「一个已知名字都没提到」的那几条引导问题。

    判据是「至少提到一个已知实体名、别名或实体类型名」。这不是完整的可答性
    证明——那要真跑一遍问答管线，慢，而且会在保存按钮上烧 LLM 调用。但它挡住
    绝大多数「问了个本体里根本没有的东西」，比如没有「库存」类型时的
    「库存多少？」。

    实体类型名也算：「产品有哪些口味？」里的两个词都是类型名不是实体名，
    只认实体名的话，本体里最典型的那类问题会被判成不可答。

    大小写不敏感：「beer 是什么」和「Beer 是什么」是同一个问题，
    敏感匹配只会让审核员反复困惑于为什么保存不了。
    """
    known: set[str] = set()
    for term in terms:
        known.add(term.standard_name.lower())
        known.update(alias.lower() for alias in term.aliases)
        known.add(term.term_type.lower())
    unmatched: list[str] = []
    for question in questions:
        lowered = question.lower()
        if not any(name and name in lowered for name in known):
            unmatched.append(question)
    return unmatched
```

- [ ] **Step 5: 跑测试确认它绿**

Expected: 6 passed

- [ ] **Step 6: 给 `tenant_personas_store` 加 `set_questions` / `get_questions`**

在 `app/graphrag/tenant_personas_store.py` 追加：

```python
import json


async def set_questions(
    conn: aiosqlite.Connection, *, tenant_id: str, questions: list[str]
) -> None:
    """写引导问题。存 JSON 数组而不是另开一张行表：它是一个有序的短列表，
    整体读整体写，拆成行表只会让「顺序」需要一个额外的列来维护。

    这里不做校验——校验在路由层（保存时要把「哪几条不通过」告诉用户，
    而这个函数只能返回成功或抛异常，说不出是哪几条）。
    """
    await conn.execute(
        "INSERT INTO tenant_personas (tenant_id, questions) VALUES (?, ?) "
        "ON CONFLICT (tenant_id) DO UPDATE SET "
        "questions = excluded.questions, updated_at = datetime('now')",
        (tenant_id, json.dumps(questions, ensure_ascii=False)),
    )
    await conn.commit()


async def get_questions(conn: aiosqlite.Connection, tenant_id: str) -> list[str]:
    """读引导问题。没配过时返回空列表。

    JSON 解析失败时也返回空列表并告警：这一列是人写进去的，历史上手工改库
    留下一个坏值是可能的，而它不该让整个前台首屏 500。
    """
    cursor = await conn.execute(
        "SELECT questions FROM tenant_personas WHERE tenant_id = ?", (tenant_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        return []
    try:
        parsed = json.loads(row["questions"])
    except (TypeError, ValueError):
        logger.warning("租户 %r 的 questions 列不是合法 JSON，按「没配」处理", tenant_id)
        return []
    return [q for q in parsed if isinstance(q, str)] if isinstance(parsed, list) else []
```

文件顶部加 `import json`、`import logging` 与 `logger = logging.getLogger(__name__)`。

- [ ] **Step 7: 补 `set_questions` / `get_questions` 的用例**

在 `tests/graphrag/test_tenant_personas_store.py` 追加：

```python
def test_set_questions_does_not_wipe_the_face():
    """写问题不该把头像和人设清掉——它们是两个独立的编辑动作。"""

    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="t1", avatar="🛍️", tagline="我知道商品")
            await set_questions(conn, tenant_id="t1", questions=["有什么新品？"])
            persona = await get_persona(conn, "t1")
            assert persona is not None
            assert persona["avatar"] == "🛍️"
            assert persona["tagline"] == "我知道商品"
            assert await get_questions(conn, "t1") == ["有什么新品？"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_questions_keep_their_order():
    """引导问题是有序的——第一条占的位置最值钱。存成集合或按字典序排
    的话，管理员精心排的顺序就没了。"""

    async def run():
        conn = await _conn()
        try:
            ordered = ["丙", "甲", "乙"]
            await set_questions(conn, tenant_id="t1", questions=ordered)
            assert await get_questions(conn, "t1") == ordered
        finally:
            await conn.close()

    asyncio.run(run())


def test_a_corrupt_questions_column_reads_as_empty_not_as_a_crash():
    async def run():
        conn = await _conn()
        try:
            await upsert_persona(conn, tenant_id="t1", avatar="", tagline="")
            await conn.execute(
                "UPDATE tenant_personas SET questions = ? WHERE tenant_id = ?",
                ("这不是 JSON", "t1"),
            )
            await conn.commit()
            assert await get_questions(conn, "t1") == []
        finally:
            await conn.close()

    asyncio.run(run())
```

- [ ] **Step 8: 跑全部相关用例 + 变异**

```
.venv/Scripts/python.exe -m pytest tests/graphrag/test_question_validation.py tests/graphrag/test_tenant_personas_store.py -q -p no:cacheprovider
```

```bash
# 变异 A：find_unmatched_questions 里去掉 term_type 那一行
#   预期红：test_a_question_naming_a_known_type_passes
# 变异 B：去掉 .lower()
#   预期红：test_matching_ignores_case_and_surrounding_punctuation
# 变异 C：本体为空时返回 []（全部放行）
#   预期红：test_an_empty_ontology_reports_every_question
# 变异 D：set_questions 的 ON CONFLICT 里加上 avatar = ''
#   预期红：test_set_questions_does_not_wipe_the_face
```

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/question_validation.py app/graphrag/tenant_personas_store.py tests/graphrag/test_question_validation.py tests/graphrag/test_tenant_personas_store.py
git commit -m "feat(chat): 手写引导问题的存取，保存前挑出一个已知名字都没提到的"
```

---

## Task 3: 引导问题的 API

**Files:**
- Modify: `app/api/admin_personas_routes.py`
- Test: `tests/api/test_admin_personas_routes.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `generate_questions`、Task 2 的 `set_questions` / `get_questions` / `find_unmatched_questions`
- Produces:
  - `GET /api/admin/personas` 回包每项加 `questions: list[str]`（手写有就用手写，没有就自动兜底）
  - `PUT /api/admin/personas/{tenant_id}` `{avatar, tagline, questions}` → 200 / 400
  - `GET /api/admin/personas/{tenant_id}/stale-questions` → `{stale: [str]}`（看板消费）

- [ ] **Step 1: 写失败测试（追加到既有文件）**

```python
def test_handwritten_questions_win_over_generated_ones(personas_conn):
    """手写优先。两档并列显示的话，用户分不清哪条是人写的——而这两者的
    可信度差很多（spec D2）。"""


def test_generated_questions_fill_in_when_none_were_written(personas_conn):
    """一条手写都没有时才兜底。新数字人刚建好、还没人配问题时，
    前台不至于空着。"""


def test_saving_a_question_that_matches_nothing_is_refused_and_names_it(personas_conn):
    """保存被拒时必须点名是哪几条。只说「保存失败」的话，配了六条的人
    得自己一条条试出来是哪条有问题。"""
    body = _put_persona(personas_conn, questions=["Beer 是什么？", "库存多少？"])
    assert body.status_code == 400
    assert "库存多少？" in body.json()["detail"]
    assert "Beer 是什么？" not in body.json()["detail"]


def test_a_refused_save_writes_nothing(personas_conn):
    """校验不通过时一条都不存——存一半的话，界面上会显示一组用户从没
    确认过的问题。"""


def test_saving_the_face_alone_does_not_clear_the_questions(personas_conn):
    """只改头像时 questions 不该被清空。"""


def test_stale_questions_endpoint_reports_the_ones_that_stopped_matching(personas_conn):
    """本体改动导致某条手写问题不再命中时，它不是默默消失——看板要能
    问出来还剩几条失效的（spec 前台第二条硬规矩）。"""


def test_a_member_cannot_write_another_tenants_persona(personas_conn):
    """写入端点必须走 require_tenant_access。读端点按 accessible 过滤，
    写端点却不校验的话，member 能改别人数字人的脸。"""
    assert _put_persona(personas_conn, tenant_id="secret", role="member").status_code == 403
```

补全成可运行用例。

- [ ] **Step 2: 跑测试确认它红**

- [ ] **Step 3: 写实现**

在 `app/api/admin_personas_routes.py` 中：

`Persona` 模型加 `questions: list[str]`。`list_my_personas` 里对每个租户：

```python
        handwritten = await get_questions(review_conn, t["tenant_id"])
        # 手写优先，一条都没有时才自动兜底。两档并列显示的话用户分不清哪条
        # 是人写的，而这两者的可信度差很多（spec D2）。
        questions = handwritten or await generate_questions(
            review_conn, graph_client, tenant_id=t["tenant_id"]
        )
```

**注意 N+1**：右栏有 N 个数字人，逐个 `generate_questions` 就是 N 次图查询。
右栏本身**不需要** questions——它只显示名字和头像。所以：

- `GET /api/admin/personas` **不带** questions（保持 Task 5 那一版的形状）
- 新增 `GET /api/admin/personas/{tenant_id}` 只返回当前那一个的完整信息，含 questions

前台只对当前数字人请求这一个。切换数字人时重新请求。这样图查询永远是 1 次而不是 N 次。

写入端点：

```python
@router.put("/{tenant_id}")
async def write_persona(
    tenant_id: str = Depends(deps.require_tenant_access),
    payload: PersonaWriteRequest = ...,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> PersonaDetail:
    """写数字人的脸和引导问题。

    走 require_tenant_access：读端点按 accessible 过滤，写端点不校验的话，
    member 能改别人数字人的脸。

    校验不通过时**一条都不存**，并且**点名是哪几条**。只说「保存失败」的话，
    配了六条的人得自己一条条试出来是哪条有问题。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    terms = await list_terms_merged(review_conn, tenant_id)
    unmatched = find_unmatched_questions(payload.questions, terms)
    if unmatched:
        raise HTTPException(
            status_code=400,
            detail=(
                "这几条引导问题在当前本体里一个已知名字都没提到，"
                f"点了大概率答不出来：{'、'.join(unmatched)}"
            ),
        )
    await upsert_persona(
        review_conn, tenant_id=tenant_id, avatar=payload.avatar, tagline=payload.tagline
    )
    await set_questions(review_conn, tenant_id=tenant_id, questions=payload.questions)
    ...
```

失效检测端点：

```python
@router.get("/{tenant_id}/stale-questions")
async def list_stale_questions(
    tenant_id: str = Depends(deps.require_tenant_access),
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, list[str]]:
    """当前本体下已经不再命中的手写引导问题。

    保存时校验过不代表永远有效——本体后来改了、实体被删了，那条问题就
    失效了。它不该默默消失（用户会以为自己没配过），而是在看板上变成一条
    待办（spec 前台硬规矩之二）。
    """
    handwritten = await get_questions(review_conn, tenant_id)
    terms = await list_terms_merged(review_conn, tenant_id)
    return {"stale": find_unmatched_questions(handwritten, terms)}
```

- [ ] **Step 4: 跑测试确认它绿 + 跑全量 API**

```
.venv/Scripts/python.exe -m pytest tests/api -q -p no:cacheprovider
```

- [ ] **Step 5: 变异验证**

```bash
# 变异 A：questions = generate(...) or handwritten（顺序反过来）
#   预期红：test_handwritten_questions_win_over_generated_ones
# 变异 B：校验不通过时只 raise 不含 unmatched 内容
#   预期红：test_saving_a_question_that_matches_nothing_is_refused_and_names_it
# 变异 C：先 upsert_persona 再校验（存一半）
#   预期红：test_a_refused_save_writes_nothing
# 变异 D：写端点的 require_tenant_access 换成 require_admin_session
#   预期红：test_a_member_cannot_write_another_tenants_persona
```

- [ ] **Step 6: 重启后端 + 提交**

```bash
git add app/api/admin_personas_routes.py tests/api/test_admin_personas_routes.py
git commit -m "feat(api): 引导问题读写端点，保存不通过时点名是哪几条"
```

---

## Task 4: 前台展示引导问题

**Files:**
- Create: `frontend/src/components/GuidedQuestions.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx`
- Modify: `frontend/src/lib/personasApi.ts`
- Test: `frontend/src/components/guidedQuestions.test.tsx`

**Interfaces:**
- Consumes: Task 3 的 `GET /api/admin/personas/{tenant_id}` → `{tenant_id, name, avatar, tagline, questions}`
- Produces: `export function GuidedQuestions(props: { tagline: string; questions: string[]; onAsk: (q: string) => void }): JSX.Element | null`

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/guidedQuestions.test.tsx`：

```tsx
it('空会话时显示人设和引导问题', async () => {
  renderChat()
  await waitFor(() => expect(screen.getByText('我知道商品、口味和产地')).toBeTruthy())
  expect(screen.getByRole('button', { name: '有哪些无香料的洗发水？' })).toBeTruthy()
})

it('点一条引导问题就直接问出去', async () => {
  const user = userEvent.setup()
  renderChat()
  await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
  await user.click(screen.getByRole('button', { name: /无香料/ }))
  await waitFor(() =>
    expect(chatRequests.some((r) => JSON.parse(String(r.body)).question.includes('无香料'))).toBe(
      true,
    ),
  )
})

it('已经有消息之后引导区消失', async () => {
  // 引导问题是「不知道能问什么」时的帮手。对话开始之后它占的是正文的位置。
  const user = userEvent.setup()
  renderChat()
  await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
  await user.click(screen.getByRole('button', { name: /无香料/ }))
  await waitFor(() => expect(screen.queryByRole('button', { name: /无香料/ })).toBeNull())
})

it('一条引导问题都没有时整块不渲染，不是渲染一个空标题', async () => {
  personaDetail = { ...PERSONA, questions: [] }
  renderChat()
  await waitFor(() => expect(screen.getByText(PERSONA.tagline)).toBeTruthy())
  expect(screen.queryByTestId('guided-questions')).toBeNull()
})

it('切换数字人之后引导问题换成新那个的', async () => {
  // 每个数字人守一张不同的图，引导问题必须跟着换。不换的话，
  // 用户在采购老王那里看到导购小美的问题，点了必然答不出来。
  const user = userEvent.setup()
  renderChat()
  await waitFor(() => expect(screen.getByRole('button', { name: /无香料/ })).toBeTruthy())
  personaDetail = { ...PERSONA_STORE, questions: ['哪家店卖得最好？'] }
  await user.click(screen.getByRole('button', { name: /店务老张/ }))
  await waitFor(() => expect(screen.getByRole('button', { name: /卖得最好/ })).toBeTruthy())
  expect(screen.queryByRole('button', { name: /无香料/ })).toBeNull()
})
```

补全成可运行用例。

- [ ] **Step 2: 跑测试确认它红**

Run: `cd frontend && npx vitest run src/components/guidedQuestions.test.tsx`

- [ ] **Step 3: 写组件**

创建 `frontend/src/components/GuidedQuestions.tsx`：

```tsx
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface GuidedQuestionsProps {
  tagline: string
  questions: string[]
  onAsk: (question: string) => void
}

/**
 * 空会话时的引导：这个数字人是谁、可以问它什么。
 *
 * 一条问题都没有时整块不渲染——一个只有标题没有内容的区块，
 * 读起来像「加载失败」。人设那一行由调用方单独渲染，它即使没有问题也该出现。
 *
 * 这里的每一条都保证点了能答出来：手写那批保存时跑过实体匹配，自动那批
 * 只用图里真有边的类型组合（见后端 guided_questions.py）。前端不做二次
 * 判断——判断的依据（本体、图）都在后端手里。
 */
export function GuidedQuestions({ tagline, questions, onAsk }: GuidedQuestionsProps) {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-3 p-6">
      <p className="text-sm text-ink-soft">{tagline}</p>
      {questions.length > 0 && (
        <div data-testid="guided-questions" className="flex flex-wrap gap-2">
          {questions.map((question) => (
            <button
              key={question}
              type="button"
              onClick={() => onAsk(question)}
              className={`cursor-pointer rounded-chip border border-subtle bg-card px-3 py-1.5 text-sm text-ink transition hover:bg-interactive-hover active:scale-95 ${focusRing}`}
            >
              {question}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
```

- [ ] **Step 4: 接进 ChatPage**

在 `ChatWorkspace` 里：当 `messages.length === 0` 时，在 `<Hero />` 之下渲染
`<GuidedQuestions ... />`，`onAsk` 直接接 `sendQuestion`。

`personasApi.ts` 加：

```ts
export interface PersonaDetail extends Persona {
  questions: string[]
}

export async function fetchPersonaDetail(
  sessionToken: string,
  tenantId: string,
): Promise<PersonaDetail> {
  const response = await adminFetch(
    `/api/admin/personas/${encodeURIComponent(tenantId)}`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '数字人信息加载失败'))
  }
  return (await response.json()) as PersonaDetail
}
```

在 `ChatWorkspace` 里对**当前那一个** tenant 调 `fetchPersonaDetail`，
effect 依赖 `[tenantId]`——切换数字人时重新拉。

**不要**在 `GET /api/admin/personas` 的回包里带 questions（Task 3 Step 3 已说明理由：
N 个数字人就是 N 次图查询）。

- [ ] **Step 5: 跑测试 + 类型检查 + 前端全量**

```
cd frontend && npx vitest run src/components/guidedQuestions.test.tsx
cd frontend && npx tsc --noEmit
cd frontend && npx vitest run src
```

既有用例可能因多了一个 fetch 而红——给它们的 stub 补 `/api/admin/personas/` 分支，
**不要**给组件加静默降级。

- [ ] **Step 6: 变异验证**

```bash
# 变异 A：questions.length > 0 判断去掉
#   预期红：test「一条引导问题都没有时整块不渲染」
# 变异 B：fetchPersonaDetail 的 effect 依赖数组去掉 tenantId
#   预期红：test「切换数字人之后引导问题换成新那个的」
# 变异 C：messages.length === 0 改成恒真
#   预期红：test「已经有消息之后引导区消失」
```

- [ ] **Step 7: 提交**

```bash
git add frontend/src/components/GuidedQuestions.tsx frontend/src/components/guidedQuestions.test.tsx frontend/src/lib/personasApi.ts frontend/src/pages/ChatPage.tsx
git commit -m "feat(chat): 空会话时给出引导问题，切数字人跟着换"
```

---

## Task 5: 后台的数字人编辑页

**Files:**
- Create: `frontend/src/admin/PersonaEditorPage.tsx`
- Modify: `frontend/src/App.tsx`（加路由）
- Modify: `frontend/src/adminRoutes.ts`（加到「本体创建」组，本阶段先挂在 model 组下）
- Test: `frontend/src/admin/personaEditor.test.tsx`

**Interfaces:**
- Consumes: Task 3 的 `GET /api/admin/personas/{tenant_id}` 与 `PUT /api/admin/personas/{tenant_id}`
- Produces: 路由 `ADMIN_ROUTES.persona = '/admin/model/persona'`（阶段三重排导航时会改成 `/admin/ontology/persona`，届时加垫片）

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/admin/personaEditor.test.tsx`：

```tsx
it('保存被拒时把后端点名的那几条原样显示出来', async () => {
  // 「这几条引导问题在当前本体里一个已知名字都没提到…：库存多少？」
  // 这句话里有用户需要的全部信息。包装成「保存失败」等于把它扔掉。
  putStatus = 400
  putDetail = '这几条引导问题在当前本体里一个已知名字都没提到，点了大概率答不出来：库存多少？'
  const user = userEvent.setup()
  renderPersonaEditor()
  await user.click(await screen.findByRole('button', { name: '保存' }))
  await waitFor(() => expect(screen.getByText(/库存多少？/)).toBeTruthy())
})

it('保存被拒之后输入框里的内容还在', async () => {
  // 清空的话，配了六条被拒一条的人要全部重打。
})

it('能加一条、能删一条、能调顺序', async () => {
  // 顺序是有意义的——第一条占的位置最值钱。
})

it('最多六条，加到第七条时按钮禁用并说明为什么', async () => {
  // 「已达上限 6 条」而不是一个点不动的按钮。点不动且不说原因，
  // 用户会以为界面坏了。
})

it('已经失效的引导问题显示一个警示，不是静默留着', async () => {
  // 本体改了之后那条问题不再命中——编辑页要标出来，
  // 否则管理员永远不知道自己配的问题已经答不出来了。
  staleResponse = { stale: ['库存多少？'] }
  renderPersonaEditor()
  await waitFor(() => expect(screen.getByText(/已失效/)).toBeTruthy())
})
```

补全成可运行用例。

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

实现要点：
- 头像用一个 emoji 输入框（`maxLength={2}`），跟 Artifact favicon 同一个形状
- 人设一行文本
- 引导问题是一个可增删排序的列表，上限 6 条
- 挂载时并发拉 `GET /personas/{tenant_id}` 与 `GET /personas/{tenant_id}/stale-questions`
- 失效的那几条在列表里带一个「已失效」标记 + 一句「本体改动之后这条不再命中，点了答不出来」

- [ ] **Step 5: 跑前端全量 + 类型检查 + 变异**

```bash
# 变异 A：错误处理改成显示固定文案「保存失败」
#   预期红：test「保存被拒时把后端点名的那几条原样显示出来」
# 变异 B：保存失败时清空表单
#   预期红：test「保存被拒之后输入框里的内容还在」
# 变异 C：stale 标记不渲染
#   预期红：test「已经失效的引导问题显示一个警示」
```

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/PersonaEditorPage.tsx frontend/src/admin/personaEditor.test.tsx frontend/src/App.tsx frontend/src/adminRoutes.ts
git commit -m "feat(admin): 数字人编辑页，失效的引导问题当场标出来"
```

---

## Task 6: 收尾验证

- [ ] **Step 1: 后端全量**

```bash
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > "$TEMP/full.txt" 2>&1
tail -5 "$TEMP/full.txt"
```

- [ ] **Step 2: 前端全量 + 类型检查**

```
cd frontend && npx vitest run src
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: 重启前后端并手工走一遍**

六项，逐项确认：

1. 后台数字人编辑页配三条引导问题 → 保存成功
2. 故意配一条「库存多少？」（本体里没有库存）→ 保存被拒，**且消息里点名这一条**
3. 前台开新会话 → 三条问题出现，点一条能答出来
4. 切到另一个数字人 → 引导问题换了
5. 一条手写都没配的数字人 → 出现自动生成的问题，点了能答出来
6. 一个刚建、没导数据的空租户 → 引导区**空着**（而不是一屏答不出来的问题）

第 2 和第 6 项是这个计划的核心，**不能跳过**。

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D2 手写优先，没配就从本体兜底 | Task 3 Step 3 |
| D2 裁决：自动那档只用图里真有数据的组合 | Task 1 |
| 硬规矩一：点了必须能答出来（手写侧） | Task 2 + Task 3 |
| 硬规矩二：失效了必须有人知道 | Task 3 的 stale 端点 + Task 5 的编辑页标记 |
| 前台空会话时展示 | Task 4 |

**2. Placeholder scan**：Task 3 Step 1、Task 5 Step 1 的用例给的是骨架 + 每条的理由，
需按仓库既有夹具补全（点名了照抄哪个文件）。Task 5 Step 2–4 合并成一段实现要点而非
逐行代码——它是一个常规表单页，仓库里有多个同形状的页面可照抄，逐行写出来反而会和
既有样式约定打架。每条用例的断言意图都写全了，没有 TBD。

**3. Type consistency**

- `generate_questions(conn, graph_client, *, tenant_id, limit) -> list[str]`：Task 1 定义，Task 3 消费。
- `find_unmatched_questions(questions, terms) -> list[str]`：Task 2 定义，Task 3 两处消费。
- `set_questions` / `get_questions`：Task 2 定义，Task 3 消费。
- `PersonaDetail = Persona & { questions: string[] }`：Task 3 后端与 Task 4 前端一致。

---

## 已知的执行前不确定项

1. **`add_allowed_combination` 的关键字参数与前置登记**（Task 1 Step 2）：读
   `ontology_constraints.py:120` 与它调用的 `_validate_references`。
   用例里的类型/关系可能需要先登记才能加白名单。
2. **`list_terms_merged` 的真实签名**（Task 3）：`admin_terms_routes.py` 已在用，照抄那里的调用。
3. **既有前台用例的 stub 需要补新分支**（Task 4 Step 5）：补 stub，不要给组件加静默降级。

`ontology_constraints` 的函数名和 `Term` 的字段已在写计划时核对完毕，结论写进了对应的 Step。
