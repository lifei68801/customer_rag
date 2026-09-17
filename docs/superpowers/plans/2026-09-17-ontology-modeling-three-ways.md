# 本体建模三种方式 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 侧边栏「本体创建」下第一项变成「本体建模」一个页面，页内三个 tab——模板构建（现建模工作台）、手动构建（现本体结构）、智能创建（新：LLM 访谈出骨架 + 问题清单校准）——三者各自独立、都写进同一份草稿。

**Architecture:** 后端新增访谈会话存储（一租户一份，与建模工作区同形）、一个纯逻辑模块（把 LLM 回复解析成骨架增量、去重、校验、算问题清单的缺口）和六个端点；LLM 调用照 `llm_extractor.py` 的写法（超时/异常/非 JSON 一律降级并说明）。前端新增 `OntologyModelingPage` 只做 tab 切换（`?way=`），两个既有页面降级为 tab 内容（去掉自己的 h1），新增 `SmartCreatePanel`；旧路由全部变重定向。

**Tech Stack:** FastAPI + aiosqlite + `ProviderRegistry`（后端）；React 18 + TypeScript + react-router `useSearchParams` + vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-09-17-ontology-modeling-three-ways-design.md`

## Global Constraints

- **三者不共享状态**（spec 决策 2）：智能创建有自己的 `ontology_interview_sessions` 表，不碰 `ontology_modeling_workspaces`；两个 tab 之间没有任何 import 关系。
- **写草稿只走既有 `POST /api/admin/ontology/{tenant_id}/draft/replace`**，写前必须先 `apply-preview` 看 diff、有删除项弹 `useConfirm`（与模板构建同一条规则）。
- **LLM 的每个元素必须带 `rationale`**，没带的丢弃；名字不合规（关系类型 `^[A-Z][A-Z0-9_]{0,63}$`、字段名 `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`、`value_type ∈ EXTRA_FIELD_VALUE_TYPES`）的那一条丢弃、其余保留；界面一律标"这是猜的"。
- **LLM 超时/异常/非 JSON 不静默**：这一轮不加元素，返回一句可读的说明；访谈超时 60 秒。
- **`missing` 由后端算**，不信 LLM 自报。
- **第一问写死**，不调 LLM。
- **智能创建不产出 ETL 映射**，`etl_mapping` 传 null。
- **旧路径全部重定向**：`/admin/ontology/ontology` → `/admin/ontology/modeling?way=manual`，`/admin/ontology/guided` → `?way=template`；第三代的 `/admin/model/ontology`、`/admin/model/guided` 同样改指向新地址。
- **缺省 tab 是手动构建**。
- **版本切换器只在手动构建 tab**（它已经在 `OntologySchemaPage` 里，不动；本体图页保留自己的）。
- 路由形状测试口径不变：新路由前缀 `/api/admin/ontology/{tenant_id}/`，挂 `tenant_scoped`。
- 进程约束沿用：`git add` 逐文件点名、不碰 `docs/superpowers/` 下用户未提交改动、不推 origin、不跑 `npx prettier --write`；后端测试 `PYTHONIOENCODING=utf-8 python -u -m pytest <path> -q`；前端 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run <path> --maxWorkers=2`；每个关键行为做变异测试；注释解释"为什么"、不写未经验证的因果；commit 结尾 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。
- **Bash 工具里写含 `\n` 字面量的代码要小心**：本会话实测 heredoc 会把 `\\n` 吞成真换行，改测试文件优先用 Write/Edit 工具，或用 `chr(92)` 拼。

## File Structure

**后端（新建）**
- `app/graphrag/ontology_interview_store.py` — 表、`get/create/save/delete_session`、`validate_state`、`initial_state()`
- `app/graphrag/ontology_interview.py` — `OPENING_QUESTION`、`parse_turn_reply`、`merge_additions`、`ask_next`、`infer_needs`、`compute_missing`
- `app/api/admin_interview_routes.py` — 六个端点
- 测试：`tests/graphrag/test_ontology_interview_store.py`、`tests/graphrag/test_ontology_interview.py`、`tests/api/test_admin_interview_routes.py`

**后端（修改）**
- `app/graphrag/ontology_lifecycle.py` — `ensure_ontology_schema` 多建一张表
- `app/main.py` — `tenant_scoped.include_router(admin_interview_router)`

**前端（新建）**
- `frontend/src/admin/ontologyModeling/OntologyModelingPage.tsx` — h1「本体建模」+ 三个 tab（`?way=`）
- `frontend/src/admin/ontologyModeling/smart/types.ts`、`interviewApi.ts`、`SmartCreatePanel.tsx`、`skeletonEdits.ts`（智能创建自己的一份，不 import 模板构建的）
- 测试：`ontologyModeling/modelingPage.test.tsx`、`ontologyModeling/smart/interviewApi.test.ts`、`smartCreate.test.tsx`

**前端（修改）**
- `frontend/src/adminRoutes.ts` — 路由键 `ontology`/`guidedOntology` → `ontologyModeling`；新增 `modelingWay(way)`；重定向表；`NAV_GROUPS`；`TENANT_SCOPED_ROUTE_KEYS`
- `frontend/src/App.tsx` — 路由
- `frontend/src/admin/OntologySchemaPage.tsx` / `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx` — 去掉自己的 h1，变成 tab 内容
- 所有引用 `ADMIN_ROUTES.ontology` / `ADMIN_ROUTES.guidedOntology` 的页面与测试（Task 4 有清单）

---

### Task 1: 访谈会话存储

**Files:**
- Create: `app/graphrag/ontology_interview_store.py`
- Modify: `app/graphrag/ontology_lifecycle.py`（import 区 + `ensure_ontology_schema`）
- Test: `tests/graphrag/test_ontology_interview_store.py`

**Interfaces:**
- Produces:
  - `ensure_interview_schema(conn)`
  - `@dataclass(frozen=True) InterviewSession(tenant_id, state: dict, updated_at, updated_by)` + `to_dict()`
  - `initial_state() -> dict`（`turns: [开场问题], skeleton: {三个空列表}, questions: [], done: False`）
  - `validate_state(state) -> dict`（抛 `InvalidInterviewStateError`）
  - `async get_session / create_session(conn, tenant_id, *, actor, now) / save_session(conn, tenant_id, *, state, expected_updated_at, actor, now) / delete_session`
  - 异常 `InterviewExistsError` / `InterviewNotFoundError` / `InterviewConflictError` / `InvalidInterviewStateError`
- Consumes: Task 2 的 `OPENING_QUESTION`（`initial_state` 用它当第一轮）——**为避免循环 import，`OPENING_QUESTION` 定义在本模块**，Task 2 从这里 import。

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_interview_store.py
from __future__ import annotations

import aiosqlite
import pytest

from app.graphrag.ontology_interview_store import (
    OPENING_QUESTION,
    InterviewConflictError,
    InterviewExistsError,
    InterviewNotFoundError,
    InvalidInterviewStateError,
    create_session,
    delete_session,
    ensure_interview_schema,
    get_session,
    initial_state,
    save_session,
    validate_state,
)
from app.graphrag.ontology_lifecycle import ensure_ontology_schema

pytestmark = pytest.mark.anyio


async def _conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_interview_schema(conn)
    return conn


def test_initial_state_starts_with_the_fixed_opening_question():
    state = initial_state()
    # 第一问写死：省一次 LLM 调用，而且第一问没有猜错的空间
    assert state["turns"] == [{"role": "assistant", "text": OPENING_QUESTION}]
    assert state["skeleton"] == {"term_types": [], "relation_types": [], "constraints": []}
    assert state["questions"] == []
    assert state["done"] is False


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ([], "映射"),
        ({"turns": {}}, "turns"),
        ({"turns": [{"role": "system", "text": "x"}]}, "system"),
        ({"turns": [{"role": "user"}]}, "text"),
        ({"skeleton": []}, "skeleton"),
        ({"skeleton": {"term_types": [{"value": "SKU", "review": "maybe"}]}}, "maybe"),
        ({"skeleton": {"term_types": [{"review": "pending"}]}}, "value"),
        ({"skeleton": {"relation_types": [{"relation_type": "X"}]}}, "review"),
        ({"skeleton": {"constraints": [{"subject": "A", "relation": "R", "review": "pending"}]}}, "object"),
        ({"questions": [{"needs": {}}]}, "text"),
        ({"done": "yes"}, "done"),
    ],
)
def test_validate_state_rejects_malformed_state(bad, fragment):
    with pytest.raises(InvalidInterviewStateError) as exc_info:
        validate_state(bad)
    assert fragment in str(exc_info.value)


def test_validate_state_fills_missing_keys():
    state = validate_state({"turns": []})
    assert state["skeleton"] == {"term_types": [], "relation_types": [], "constraints": []}
    assert state["questions"] == []
    assert state["done"] is False


async def test_create_then_get_round_trips():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="2026-09-17T10:00:00")
    assert created.updated_by == "alice"
    assert created.state["turns"][0]["text"] == OPENING_QUESTION
    assert await get_session(conn, "t1") == created


async def test_get_absent_returns_none():
    conn = await _conn()
    assert await get_session(conn, "t1") is None


async def test_create_twice_raises():
    conn = await _conn()
    await create_session(conn, "t1", actor="a", now="n")
    with pytest.raises(InterviewExistsError):
        await create_session(conn, "t1", actor="a", now="n2")


async def test_save_is_optimistically_locked_and_atomic():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="2026-09-17T10:00:00")
    saved = await save_session(
        conn, "t1",
        state={"turns": [{"role": "assistant", "text": "q"}, {"role": "user", "text": "a"}]},
        expected_updated_at=created.updated_at, actor="bob", now="2026-09-17T10:01:00",
    )
    assert saved.updated_by == "bob"
    with pytest.raises(InterviewConflictError):
        await save_session(
            conn, "t1", state={"turns": []},
            expected_updated_at=created.updated_at, actor="carol", now="2026-09-17T10:02:00",
        )
    assert (await get_session(conn, "t1")).updated_by == "bob"


async def test_save_absent_raises_not_found_even_with_invalid_state():
    conn = await _conn()
    with pytest.raises(InterviewNotFoundError):
        await save_session(conn, "t1", state={"turns": {}}, expected_updated_at="x", actor="a", now="n")


async def test_save_rejects_invalid_state_without_writing():
    conn = await _conn()
    created = await create_session(conn, "t1", actor="alice", now="n")
    with pytest.raises(InvalidInterviewStateError):
        await save_session(conn, "t1", state={"done": "yes"}, expected_updated_at=created.updated_at, actor="bob", now="n2")
    assert (await get_session(conn, "t1")).updated_by == "alice"


async def test_delete_is_idempotent():
    conn = await _conn()
    await create_session(conn, "t1", actor="a", now="n")
    await delete_session(conn, "t1")
    assert await get_session(conn, "t1") is None
    await delete_session(conn, "t1")


async def test_ensure_ontology_schema_creates_the_table():
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ontology_interview_sessions'"
    )
    assert await cursor.fetchone() is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_interview_store.py -q`
Expected: `ModuleNotFoundError: app.graphrag.ontology_interview_store`

- [ ] **Step 3: 写存储模块**

```python
# app/graphrag/ontology_interview_store.py
"""智能创建的访谈会话：一个租户一份、长期存在、可续。

跟建模工作区（ontology_modeling_workspace.py）同一个取向、同一套乐观锁，
但**不共用表、不共用代码**：spec 决策 2 定了三种构建方式各自独立，共用
存储会让"独立"在第一次 schema 改动时就名存实亡。

state_json 对本模块不透明：只校验外形，语义（元素之间引用得上不上）留给
写草稿那一刻的 replace_draft 去查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import aiosqlite

#: 第一问写死（spec 决策 4）：省一次 LLM 调用，而且第一问没有猜错的余地。
OPENING_QUESTION = "先说说你们主要做什么生意？卖什么、卖给谁、通过什么渠道。"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ontology_interview_sessions (
    tenant_id   TEXT NOT NULL PRIMARY KEY,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);
"""

_ROLES = frozenset({"assistant", "user"})
_REVIEWS = frozenset({"pending", "accepted", "rejected"})


class InterviewExistsError(Exception):
    """这个租户已经有访谈会话了。"""


class InterviewNotFoundError(Exception):
    """这个租户还没有访谈会话。"""


class InterviewConflictError(Exception):
    """带来的 updated_at 不是库里那一版。"""


class InvalidInterviewStateError(Exception):
    """state_json 外形不合法。"""


@dataclass(frozen=True)
class InterviewSession:
    tenant_id: str
    state: dict
    updated_at: str
    updated_by: str

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "state": self.state,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


async def ensure_interview_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(_SCHEMA_SQL)
    await conn.commit()


def initial_state() -> dict:
    return {
        "turns": [{"role": "assistant", "text": OPENING_QUESTION}],
        "skeleton": {"term_types": [], "relation_types": [], "constraints": []},
        "questions": [],
        "done": False,
    }


def _require_keys(item: object, keys: tuple[str, ...], *, where: str) -> dict:
    if not isinstance(item, dict):
        raise InvalidInterviewStateError(f"{where} 的每一项要是映射，收到: {item!r}")
    for key in keys:
        if not isinstance(item.get(key), str) or not item[key]:
            raise InvalidInterviewStateError(f"{where} 缺少非空字符串字段 {key}: {item!r}")
    return item


def _check_review(item: dict, *, where: str) -> None:
    review = item.get("review")
    if review not in _REVIEWS:
        raise InvalidInterviewStateError(f"{where} 的 review {review!r} 不合法，只接受 {sorted(_REVIEWS)}")


def validate_state(state: object) -> dict:
    """校验外形并补齐缺失的顶层键。语义不在这里查（理由见模块 docstring）。"""
    if not isinstance(state, dict):
        raise InvalidInterviewStateError(f"访谈状态要是映射，收到: {type(state).__name__}")
    turns = state.get("turns", [])
    if not isinstance(turns, list):
        raise InvalidInterviewStateError(f"访谈状态的 turns 要是列表，收到: {turns!r}")
    for turn in turns:
        _require_keys(turn, ("role", "text"), where="turns")
        if turn["role"] not in _ROLES:
            raise InvalidInterviewStateError(f"turns 的 role {turn['role']!r} 不合法，只接受 {sorted(_ROLES)}")

    skeleton = state.get("skeleton", {})
    if not isinstance(skeleton, dict):
        raise InvalidInterviewStateError(f"访谈状态的 skeleton 要是映射，收到: {skeleton!r}")
    normalized_skeleton: dict = {}
    for key in ("term_types", "relation_types", "constraints"):
        items = skeleton.get(key, [])
        if not isinstance(items, list):
            raise InvalidInterviewStateError(f"skeleton 的 {key} 要是列表，收到: {items!r}")
        normalized_skeleton[key] = items
    for item in normalized_skeleton["term_types"]:
        _require_keys(item, ("value",), where="skeleton.term_types")
        _check_review(item, where=f"skeleton.term_types[{item['value']!r}]")
    for item in normalized_skeleton["relation_types"]:
        _require_keys(item, ("relation_type",), where="skeleton.relation_types")
        _check_review(item, where=f"skeleton.relation_types[{item['relation_type']!r}]")
    for item in normalized_skeleton["constraints"]:
        _require_keys(item, ("subject", "relation", "object"), where="skeleton.constraints")
        _check_review(item, where=f"skeleton.constraints[{item!r}]")

    questions = state.get("questions", [])
    if not isinstance(questions, list):
        raise InvalidInterviewStateError(f"访谈状态的 questions 要是列表，收到: {questions!r}")
    for question in questions:
        _require_keys(question, ("text",), where="questions")

    done = state.get("done", False)
    if not isinstance(done, bool):
        raise InvalidInterviewStateError(f"访谈状态的 done 要是布尔，收到: {done!r}")

    return {"turns": turns, "skeleton": normalized_skeleton, "questions": questions, "done": done}


def _row_to_session(tenant_id: str, row) -> InterviewSession:
    return InterviewSession(tenant_id=tenant_id, state=json.loads(row[0]), updated_at=row[1], updated_by=row[2])


async def get_session(conn: aiosqlite.Connection, tenant_id: str) -> InterviewSession | None:
    cursor = await conn.execute(
        "SELECT state_json, updated_at, updated_by FROM ontology_interview_sessions WHERE tenant_id = ?",
        (tenant_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else _row_to_session(tenant_id, row)


async def create_session(conn: aiosqlite.Connection, tenant_id: str, *, actor: str, now: str) -> InterviewSession:
    if await get_session(conn, tenant_id) is not None:
        raise InterviewExistsError(f"租户 {tenant_id} 已经有访谈会话了")
    state = initial_state()
    try:
        await conn.execute(
            "INSERT INTO ontology_interview_sessions (tenant_id, state_json, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (tenant_id, json.dumps(state, ensure_ascii=False), now, actor),
        )
    except aiosqlite.IntegrityError as exc:
        # 上面那次查和这次插之间被别人抢先了：主键撞上，报"已存在"而不是 500。
        raise InterviewExistsError(f"租户 {tenant_id} 已经有访谈会话了") from exc
    await conn.commit()
    return InterviewSession(tenant_id=tenant_id, state=state, updated_at=now, updated_by=actor)


async def save_session(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    state: dict,
    expected_updated_at: str,
    actor: str,
    now: str,
) -> InterviewSession:
    """整份写回，原子乐观锁（UPDATE … WHERE updated_at = ?）。

    先查存在性再校验 state：客户端拿到 404 才知道该重新开始，拿到 400 会去
    改 state。校验放在写之前：失败时库里还是上一版，不需要事务。
    """
    if await get_session(conn, tenant_id) is None:
        raise InterviewNotFoundError(f"租户 {tenant_id} 还没有访谈会话")
    normalized = validate_state(state)
    cursor = await conn.execute(
        "UPDATE ontology_interview_sessions SET state_json = ?, updated_at = ?, updated_by = ? "
        "WHERE tenant_id = ? AND updated_at = ?",
        (json.dumps(normalized, ensure_ascii=False), now, actor, tenant_id, expected_updated_at),
    )
    if cursor.rowcount == 0:
        current = await get_session(conn, tenant_id)
        if current is None:
            raise InterviewNotFoundError(f"租户 {tenant_id} 还没有访谈会话")
        raise InterviewConflictError(
            f"访谈在 {current.updated_at} 被 {current.updated_by} 改过，你手上这份是 {expected_updated_at} 的。刷新后重试。"
        )
    await conn.commit()
    return InterviewSession(tenant_id=tenant_id, state=normalized, updated_at=now, updated_by=actor)


async def delete_session(conn: aiosqlite.Connection, tenant_id: str) -> None:
    await conn.execute("DELETE FROM ontology_interview_sessions WHERE tenant_id = ?", (tenant_id,))
    await conn.commit()
```

- [ ] **Step 4: 挂进统一建表入口**

`app/graphrag/ontology_lifecycle.py` import 区加 `from app.graphrag.ontology_interview_store import ensure_interview_schema`；`ensure_ontology_schema` 里 `await ensure_modeling_workspace_schema(conn)` 之后加 `await ensure_interview_schema(conn)`；docstring 里"五张表"改"六张表"，补一句：访谈会话跟建模工作区同理，跟本体同寿。

- [ ] **Step 5: 跑测试确认通过**；**Step 6: 变异**：把 `save_session` 的 `WHERE ... AND updated_at = ?` 连同参数去掉 → `test_save_is_optimistically_locked_and_atomic` 变红；改回。把 `_check_review` 调用删掉 → 参数化里 `maybe` 那条变红；改回。

- [ ] **Step 7: 提交**

```bash
git add app/graphrag/ontology_interview_store.py app/graphrag/ontology_lifecycle.py tests/graphrag/test_ontology_interview_store.py
git commit -m "feat(ontology): 智能创建的访谈会话存储

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 访谈逻辑（解析 LLM 回复、合并骨架、算问题缺口）

**Files:**
- Create: `app/graphrag/ontology_interview.py`
- Test: `tests/graphrag/test_ontology_interview.py`

**Interfaces:**
- Consumes: `app.providers.registry.ProviderRegistry.run(capability, request, *, provider_name)`、`app.providers.base.ProviderRequest/ProviderResult/ProviderCapability`；`app.graphrag.value_types.EXTRA_FIELD_VALUE_TYPES`；`app.graphrag.ontology_categories.EXTRA_FIELD_NAME_PATTERN`；Task 1 的 `OPENING_QUESTION`
- Produces:
  - `@dataclass(frozen=True) TurnResult(question: str | None, added: dict, dropped: list[str], done: bool, note: str | None)`——`added` 是 `{term_types, relation_types, constraints}` 三个列表（已经带 `confidence/from_turn/review`），`dropped` 是被丢弃元素的原因（给界面显示），`note` 是"这一轮没能认出新概念"这类说明
  - `parse_turn_reply(text: str, *, from_turn: int) -> TurnResult`（纯函数）
  - `merge_additions(skeleton: dict, added: dict) -> dict`（纯函数，返回新 dict；重名不重复加、追加 rationale）
  - `async ask_next(llm_registry, *, provider_name, turns: list[dict], skeleton: dict, timeout_sec: float = 60.0) -> TurnResult`
  - `async infer_needs(llm_registry, *, provider_name, question: str, skeleton: dict, timeout_sec: float = 30.0) -> dict`（`{"term_types": [...], "relation_types": [...]}`，失败给空）
  - `compute_missing(needs: dict, skeleton: dict) -> list[str]`（纯函数）

- [ ] **Step 1: 写失败的测试**

```python
# tests/graphrag/test_ontology_interview.py
from __future__ import annotations

import asyncio
import json

import pytest

from app.graphrag.ontology_interview import (
    ask_next,
    compute_missing,
    infer_needs,
    merge_additions,
    parse_turn_reply,
)
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult

pytestmark = pytest.mark.anyio


class _FakeRegistry:
    """记下请求、按脚本回话。text=None 表示抛异常，delay 表示拖时间。"""

    def __init__(self, text: str | None, *, delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.requests: list[ProviderRequest] = []

    async def run(self, capability, request, *, provider_name):
        assert capability is ProviderCapability.LLM
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.text is None:
            raise RuntimeError("provider down")
        return ProviderResult(text=self.text)


_EMPTY = {"term_types": [], "relation_types": [], "constraints": []}

_GOOD_REPLY = json.dumps({
    "question": "你们的商品有没有分品类？",
    "add": {
        "term_types": [
            {"value": "商品", "rationale": "用户说主要卖服装和家居",
             "extra_fields": [{"name": "price", "value_type": "number", "label": "价格"}]},
            {"value": "门店", "rationale": "用户提到线下渠道"},
        ],
        "relation_types": [
            {"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售", "rationale": "商品通过门店卖"},
        ],
        "constraints": [
            {"subject": "商品", "relation": "SOLD_AT", "object": "门店", "rationale": "同上"},
        ],
    },
    "done": False,
}, ensure_ascii=False)


def test_parse_turn_reply_marks_everything_as_guess_with_provenance():
    result = parse_turn_reply(_GOOD_REPLY, from_turn=1)
    assert result.question == "你们的商品有没有分品类？"
    assert result.done is False
    sku = result.added["term_types"][0]
    assert sku["value"] == "商品"
    assert sku["confidence"] == "guess"
    assert sku["from_turn"] == 1
    assert sku["review"] == "pending"
    assert sku["rationale"] == "用户说主要卖服装和家居"
    assert sku["extra_fields"] == [{"name": "price", "value_type": "number", "label": "价格"}]
    assert result.added["relation_types"][0]["relation_type"] == "SOLD_AT"
    assert result.added["constraints"][0] == {
        "subject": "商品", "relation": "SOLD_AT", "object": "门店",
        "rationale": "同上", "confidence": "guess", "from_turn": 1, "review": "pending",
    }
    assert result.dropped == []


@pytest.mark.parametrize(
    "mutate,dropped_fragment,kept",
    [
        # 没有 rationale 的元素丢弃——凭空出现的实体用户没法判断去留
        (lambda d: d["add"]["term_types"][0].pop("rationale"), "商品", "门店"),
        # 关系类型名不合规（小写）丢弃，其余保留
        (lambda d: d["add"]["relation_types"][0].__setitem__("relation_type", "sold_at"), "sold_at", "商品"),
        # 字段名不合规：整个实体丢弃（半个实体比没有更糟——ETL 会按声明的字段建索引）
        (lambda d: d["add"]["term_types"][0]["extra_fields"][0].__setitem__("name", "价 格"), "商品", "门店"),
        # value_type 不合规同理
        (lambda d: d["add"]["term_types"][0]["extra_fields"][0].__setitem__("value_type", "blob"), "商品", "门店"),
    ],
)
def test_parse_turn_reply_drops_only_the_bad_element(mutate, dropped_fragment, kept):
    payload = json.loads(_GOOD_REPLY)
    mutate(payload)
    result = parse_turn_reply(json.dumps(payload, ensure_ascii=False), from_turn=1)
    assert any(dropped_fragment in reason for reason in result.dropped)
    names = [t["value"] for t in result.added["term_types"]] + [r["relation_type"] for r in result.added["relation_types"]]
    assert kept in names


def test_parse_turn_reply_non_json_adds_nothing_and_says_so():
    result = parse_turn_reply("我觉得你们需要一个商品实体", from_turn=1)
    assert result.added == _EMPTY
    assert result.question is None
    assert result.note is not None


def test_parse_turn_reply_done_flag_stops_asking():
    result = parse_turn_reply(json.dumps({"question": None, "add": _EMPTY, "done": True}), from_turn=3)
    assert result.done is True
    assert result.question is None


def test_merge_additions_dedupes_by_name_and_appends_rationale():
    skeleton = {
        "term_types": [{"value": "商品", "rationale": "第一轮说的", "confidence": "guess", "from_turn": 1, "review": "accepted", "extra_fields": []}],
        "relation_types": [],
        "constraints": [],
    }
    added = parse_turn_reply(_GOOD_REPLY, from_turn=3).added
    merged = merge_additions(skeleton, added)
    assert [t["value"] for t in merged["term_types"]] == ["商品", "门店"]
    # 同一个概念被两轮回答佐证：加强不是冲突，review 决定保持不变，理由追加
    assert merged["term_types"][0]["review"] == "accepted"
    assert "第一轮说的" in merged["term_types"][0]["rationale"]
    assert "用户说主要卖服装和家居" in merged["term_types"][0]["rationale"]
    # 不改入参
    assert skeleton["term_types"][0]["rationale"] == "第一轮说的"


def test_merge_additions_dedupes_constraints_by_triple():
    skeleton = {"term_types": [], "relation_types": [], "constraints": [
        {"subject": "商品", "relation": "SOLD_AT", "object": "门店", "rationale": "x", "confidence": "guess", "from_turn": 1, "review": "rejected"},
    ]}
    merged = merge_additions(skeleton, parse_turn_reply(_GOOD_REPLY, from_turn=2).added)
    assert len(merged["constraints"]) == 1
    assert merged["constraints"][0]["review"] == "rejected"


async def test_ask_next_sends_history_and_skeleton_and_parses_reply():
    registry = _FakeRegistry(_GOOD_REPLY)
    turns = [{"role": "assistant", "text": "先说说你们做什么？"}, {"role": "user", "text": "我们卖服装"}]
    result = await ask_next(registry, provider_name="p", turns=turns, skeleton=_EMPTY)
    assert result.question == "你们的商品有没有分品类？"
    assert result.added["term_types"][0]["from_turn"] == 1
    request = registry.requests[0]
    # 历史轮次原样进对话；骨架进 system prompt，模型才知道哪些已经有了
    assert request.messages[-1] == {"role": "user", "content": "我们卖服装"}
    assert "先说说你们做什么" in request.messages[1]["content"]
    assert "term_types" in request.messages[0]["content"]


async def test_ask_next_timeout_adds_nothing_and_explains():
    registry = _FakeRegistry(_GOOD_REPLY, delay=0.2)
    result = await ask_next(registry, provider_name="p", turns=[{"role": "user", "text": "x"}], skeleton=_EMPTY, timeout_sec=0.05)
    assert result.added == _EMPTY
    assert result.note is not None
    assert "超时" in result.note


async def test_ask_next_provider_failure_adds_nothing_and_explains():
    registry = _FakeRegistry(None)
    result = await ask_next(registry, provider_name="p", turns=[{"role": "user", "text": "x"}], skeleton=_EMPTY)
    assert result.added == _EMPTY
    assert result.note is not None


async def test_infer_needs_returns_names_and_falls_back_to_empty():
    good = _FakeRegistry(json.dumps({"needs": {"term_types": ["品类", "商品"], "relation_types": ["BELONGS_TO"]}}, ensure_ascii=False))
    assert await infer_needs(good, provider_name="p", question="哪个品类卖得最好", skeleton=_EMPTY) == {
        "term_types": ["品类", "商品"], "relation_types": ["BELONGS_TO"],
    }
    bad = _FakeRegistry("not json")
    assert await infer_needs(bad, provider_name="p", question="q", skeleton=_EMPTY) == {"term_types": [], "relation_types": []}


def test_compute_missing_ignores_rejected_and_is_computed_here_not_by_llm():
    skeleton = {
        "term_types": [{"value": "商品", "review": "accepted"}, {"value": "品类", "review": "rejected"}],
        "relation_types": [{"relation_type": "BELONGS_TO", "review": "pending"}],
        "constraints": [],
    }
    needs = {"term_types": ["品类", "商品", "门店"], "relation_types": ["BELONGS_TO", "SOLD_AT"]}
    # 拒过的品类算缺（用户明确不要，但问题需要——这正是要摆到他面前的矛盾）
    assert compute_missing(needs, skeleton) == ["品类", "门店", "SOLD_AT"]
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```python
# app/graphrag/ontology_interview.py
"""智能创建的访谈逻辑：把 LLM 的回复翻成骨架增量，合并进骨架，算问题清单的缺口。

三条硬规则（spec 行为规格 §1-2）：
1. 每个元素必须带 rationale，没带的丢弃——凭空出现的实体用户没法判断去留。
2. 名字不合规的那一条丢弃、其余保留——一条脏数据不该让整轮白问。
3. missing 由这里算，不信 LLM 自报——它看不到 review 状态，也容易顺着问题编。

LLM 调用的降级口径照 llm_extractor.py：超时/异常/非 JSON 一律"这一轮什么
都不加"，但要把原因交回界面说出来，不静默。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from app.graphrag.ontology_categories import EXTRA_FIELD_NAME_PATTERN
from app.graphrag.value_types import EXTRA_FIELD_VALUE_TYPES
from app.providers.base import ProviderCapability, ProviderRequest

logger = logging.getLogger(__name__)

# 与 ontology_lifecycle._validate_draft_relation_type 同一条规则；那边是模块私有，
# 这里复制一份，两处要同步（取舍同 ontology_skills.py）。
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")

_EMPTY_ADDED: dict = {"term_types": [], "relation_types": [], "constraints": []}


@dataclass(frozen=True)
class TurnResult:
    question: str | None
    added: dict
    dropped: list[str] = field(default_factory=list)
    done: bool = False
    note: str | None = None


def _empty_added() -> dict:
    return {"term_types": [], "relation_types": [], "constraints": []}


def _stamp(item: dict, *, from_turn: int) -> dict:
    # 全都是猜的（spec 数据模型）：confidence 只有 guess 一个取值，留字段是为
    # 将来区分"用户明确说过"与"模型推的"。
    return {**item, "confidence": "guess", "from_turn": from_turn, "review": "pending"}


def _parse_extra_fields(raw: object, *, owner: str, dropped: list[str]) -> list[dict] | None:
    """字段有一个不合规就让整个实体作废：半个实体比没有更糟——ETL 会按声明
    的字段建索引，缺一个字段的实体落进去之后要靠人回头补。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        dropped.append(f"实体类型 {owner} 的 extra_fields 不是列表")
        return None
    fields: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            dropped.append(f"实体类型 {owner} 的某个字段不是映射")
            return None
        name = str(item.get("name", "")).strip()
        value_type = str(item.get("value_type", "")).strip()
        if not EXTRA_FIELD_NAME_PATTERN.match(name):
            dropped.append(f"实体类型 {owner} 的字段名 {name!r} 不合法（要 ASCII 标识符）")
            return None
        if value_type not in EXTRA_FIELD_VALUE_TYPES:
            dropped.append(f"实体类型 {owner} 的字段 {name} 的类型 {value_type!r} 不合法")
            return None
        fields.append({"name": name, "value_type": value_type, "label": str(item.get("label") or name)})
    return fields


def parse_turn_reply(text: str, *, from_turn: int) -> TurnResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return TurnResult(question=None, added=_empty_added(), note="这一轮没能从回答里认出新概念（模型没有按约定格式回复），可以换个说法再说一次。")
    if not isinstance(payload, dict):
        return TurnResult(question=None, added=_empty_added(), note="这一轮没能从回答里认出新概念（模型回复的形状不对），可以换个说法再说一次。")

    dropped: list[str] = []
    added = _empty_added()
    add = payload.get("add") if isinstance(payload.get("add"), dict) else {}

    for raw in add.get("term_types") or []:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not value:
            continue
        if not rationale:
            dropped.append(f"实体类型 {value} 没有给出理由，丢弃")
            continue
        fields = _parse_extra_fields(raw.get("extra_fields"), owner=value, dropped=dropped)
        if fields is None:
            continue
        added["term_types"].append(_stamp({
            "value": value,
            "display_name": str(raw.get("display_name") or value),
            "rationale": rationale,
            "extra_fields": fields,
            "standard_name_value_type": "string",
        }, from_turn=from_turn))

    for raw in add.get("relation_types") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("relation_type", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not name:
            continue
        if not rationale:
            dropped.append(f"关系类型 {name} 没有给出理由，丢弃")
            continue
        if not _RELATION_TYPE_PATTERN.match(name):
            dropped.append(f"关系类型名 {name!r} 不合法（要大写字母开头的 A-Z0-9_），丢弃")
            continue
        added["relation_types"].append(_stamp({
            "relation_type": name,
            "example_phrase": str(raw.get("example_phrase") or ""),
            "description": str(raw.get("description") or ""),
            "rationale": rationale,
        }, from_turn=from_turn))

    for raw in add.get("constraints") or []:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject", "")).strip()
        relation = str(raw.get("relation", "")).strip()
        obj = str(raw.get("object", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not (subject and relation and obj):
            continue
        if not rationale:
            dropped.append(f"约束 {subject}-{relation}-{obj} 没有给出理由，丢弃")
            continue
        added["constraints"].append(_stamp({
            "subject": subject, "relation": relation, "object": obj, "rationale": rationale,
        }, from_turn=from_turn))

    question_raw = payload.get("question")
    question = str(question_raw).strip() if isinstance(question_raw, str) and question_raw.strip() else None
    done = payload.get("done") is True
    return TurnResult(question=None if done else question, added=added, dropped=dropped, done=done)


def _merge_list(existing: list[dict], incoming: list[dict], key) -> list[dict]:
    """重名不重复加，只把新理由追加到已有那条上；review 保持用户的决定。"""
    by_key = {key(item): dict(item) for item in existing}
    order = [key(item) for item in existing]
    for item in incoming:
        k = key(item)
        if k in by_key:
            current = by_key[k]
            if item.get("rationale") and item["rationale"] not in (current.get("rationale") or ""):
                current["rationale"] = (current.get("rationale") or "").rstrip("；") + "；" + item["rationale"] if current.get("rationale") else item["rationale"]
            continue
        by_key[k] = dict(item)
        order.append(k)
    return [by_key[k] for k in order]


def merge_additions(skeleton: dict, added: dict) -> dict:
    return {
        "term_types": _merge_list(skeleton.get("term_types", []), added.get("term_types", []), key=lambda t: t["value"]),
        "relation_types": _merge_list(skeleton.get("relation_types", []), added.get("relation_types", []), key=lambda r: r["relation_type"]),
        "constraints": _merge_list(
            skeleton.get("constraints", []), added.get("constraints", []),
            key=lambda c: (c["subject"], c["relation"], c["object"]),
        ),
    }


_INTERVIEW_SYSTEM = """你是企业知识图谱的本体建模顾问，正在访谈一位企业用户，目的是弄清他们的业务里有哪些**实体类型**（人、物、单据、组织、地点这类概念）、实体之间有哪些**关系类型**，以及每种关系连接哪两类实体（约束）。

每一轮：根据用户的最新回答，把你能确认的新概念加进骨架，然后问**一个**最有信息量的下一个问题。问题要具体、口语化、一次只问一件事。当你认为骨架已经足够描述他们的核心业务时，把 done 设为 true 并不再提问。

当前骨架（已经有的不要重复加）：
{skeleton}

只输出一个 JSON 对象，不要任何多余文字：
{{"question": "下一个问题或 null", "add": {{"term_types": [{{"value": "中文名", "display_name": "可选", "rationale": "为什么从回答里推出这个", "extra_fields": [{{"name": "ascii_name", "value_type": "string|number|integer|date", "label": "中文名"}}]}}], "relation_types": [{{"relation_type": "UPPER_SNAKE", "example_phrase": "一句话例子", "rationale": "..."}}], "constraints": [{{"subject": "实体类型", "relation": "关系类型", "object": "实体类型", "rationale": "..."}}]}}, "done": false}}

规则：每个元素必须有 rationale；relation_type 只能是大写字母、数字、下划线；extra_fields 的 name 只能是 ASCII 标识符。"""


async def ask_next(llm_registry, *, provider_name: str, turns: list[dict], skeleton: dict, timeout_sec: float = 60.0) -> TurnResult:
    """把全部历史 + 当前骨架交给模型，要它加元素并问下一个问题。

    from_turn 是**用户最新那条回答**在 turns 里的下标——元素出自那一轮，
    界面上点一下能跳回去看当时说了什么。
    """
    from_turn = max((i for i, t in enumerate(turns) if t.get("role") == "user"), default=len(turns) - 1)
    messages = [{"role": "system", "content": _INTERVIEW_SYSTEM.format(skeleton=json.dumps(skeleton, ensure_ascii=False))}]
    messages.extend({"role": t["role"], "content": t["text"]} for t in turns)
    try:
        result = await asyncio.wait_for(
            llm_registry.run(ProviderCapability.LLM, ProviderRequest(messages=messages), provider_name=provider_name),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("访谈这一轮超时")
        return TurnResult(question=None, added=_empty_added(), note=f"模型 {int(timeout_sec)} 秒内没有回复（超时），这一轮没有加任何概念。稍后再试，或换个说法。")
    except Exception:
        logger.warning("访谈这一轮调用失败", exc_info=True)
        return TurnResult(question=None, added=_empty_added(), note="模型调用失败，这一轮没有加任何概念。稍后再试。")
    return parse_turn_reply(result.text, from_turn=from_turn)


_NEEDS_SYSTEM = """给你一个企业用户想在知识图谱里问的业务问题，以及当前的本体骨架。判断要回答这个问题需要哪些实体类型和关系类型（用骨架里已有的名字；骨架里没有的用你认为合适的名字）。

当前骨架：
{skeleton}

只输出一个 JSON 对象：{{"needs": {{"term_types": ["..."], "relation_types": ["UPPER_SNAKE"]}}}}"""


async def infer_needs(llm_registry, *, provider_name: str, question: str, skeleton: dict, timeout_sec: float = 30.0) -> dict:
    empty = {"term_types": [], "relation_types": []}
    messages = [
        {"role": "system", "content": _NEEDS_SYSTEM.format(skeleton=json.dumps(skeleton, ensure_ascii=False))},
        {"role": "user", "content": question},
    ]
    try:
        result = await asyncio.wait_for(
            llm_registry.run(ProviderCapability.LLM, ProviderRequest(messages=messages), provider_name=provider_name),
            timeout=timeout_sec,
        )
        payload = json.loads(result.text)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        return empty
    except Exception:
        logger.warning("问题清单反推失败", exc_info=True)
        return empty
    needs = payload.get("needs") if isinstance(payload, dict) else None
    if not isinstance(needs, dict):
        return empty
    return {
        "term_types": [str(x).strip() for x in needs.get("term_types") or [] if str(x).strip()],
        "relation_types": [str(x).strip() for x in needs.get("relation_types") or [] if str(x).strip()],
    }


def compute_missing(needs: dict, skeleton: dict) -> list[str]:
    """问题需要、骨架里没有（或被拒了）的名字。拒过的也算缺：用户明确不要
    但问题需要，这正是要摆到他面前的矛盾，不能替他藏起来。"""
    have_terms = {t["value"] for t in skeleton.get("term_types", []) if t.get("review") != "rejected"}
    have_relations = {r["relation_type"] for r in skeleton.get("relation_types", []) if r.get("review") != "rejected"}
    missing = [name for name in needs.get("term_types", []) if name not in have_terms]
    missing += [name for name in needs.get("relation_types", []) if name not in have_relations]
    return missing
```

注意 `_merge_list` 里 rationale 追加那一行写得绕，实现时简化为：

```python
            if item.get("rationale") and item["rationale"] not in (current.get("rationale") or ""):
                current["rationale"] = f"{current['rationale']}；{item['rationale']}" if current.get("rationale") else item["rationale"]
```

- [ ] **Step 4: 跑测试确认通过**；**Step 5: 变异**：(a) `parse_turn_reply` 里 `if not rationale:` 那段对 term_types 删掉 → 参数化第一条变红；(b) `compute_missing` 里 `if t.get("review") != "rejected"` 去掉 → `test_compute_missing_...` 变红。各自改回。

- [ ] **Step 6: 提交**

```bash
git add app/graphrag/ontology_interview.py tests/graphrag/test_ontology_interview.py
git commit -m "feat(ontology): 访谈逻辑——解析模型回复、合并骨架、算问题缺口

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 访谈路由

**Files:**
- Create: `app/api/admin_interview_routes.py`
- Modify: `app/main.py`（import + `tenant_scoped.include_router`）
- Test: `tests/api/test_admin_interview_routes.py`

**Interfaces:**
- Consumes: Task 1 全部；Task 2 的 `ask_next` / `merge_additions` / `infer_needs` / `compute_missing`；`deps.get_llm_registry`、`deps.DEFAULT_LLM_PROVIDER_NAME`（先 `grep -n "DEFAULT_LLM_PROVIDER_NAME" app/api/deps.py` 确认名字与取值方式；`admin_document_routes.py` 里有现成用法照抄）；`require_active_tenant_or_404`
- Produces: `router`，prefix `/api/admin/ontology`：

| 方法 | 路径 | body | 返回 |
|---|---|---|---|
| GET | `/{tenant_id}/interview` | — | `{"session": null \| {...}}` |
| POST | `/{tenant_id}/interview` | — | `{"session": {...}}`，409 已存在 |
| POST | `/{tenant_id}/interview/answer` | `{"answer": str, "updated_at": str}` | `{"session": {...}, "turn": {"question", "added_count", "dropped": [...], "note"}}`，409 冲突，404 无会话 |
| PUT | `/{tenant_id}/interview` | `{"state": {...}, "updated_at": str}` | `{"session": {...}}`，400/404/409 |
| DELETE | `/{tenant_id}/interview` | — | `{"deleted": true}` |
| POST | `/{tenant_id}/interview/questions` | `{"text": str, "updated_at": str}` | `{"session": {...}, "question": {"text", "needs", "missing"}}` |

`answer` 端点的流程：读会话（404）→ 校验 `updated_at`（不一致 409，**先于**调 LLM——调完再发现冲突等于白等一分钟）→ 把 `{"role": "user", "text": answer}` 追加进 `turns` → `ask_next` → `merge_additions` → 若有 `question` 追加 `{"role": "assistant", "text": question}`，`done` 写进 state → `save_session`（此时 `expected_updated_at` 用刚才读到的那一版）→ 返回。

- [ ] **Step 1: 写失败的测试**

```python
# tests/api/test_admin_interview_routes.py
from __future__ import annotations

import json

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.admin_session import AdminSession
from app.graphrag.ontology_lifecycle import ensure_ontology_schema
from app.graphrag.tenants_store import create_tenant, create_tenants_table
from app.main import app
from app.providers.base import ProviderResult

pytestmark = pytest.mark.anyio

AUTH = {"Authorization": "Bearer x"}


class _ScriptedLLM:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.calls = 0

    async def run(self, capability, request, *, provider_name):
        self.calls += 1
        return ProviderResult(text=self.replies.pop(0))


async def _review_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await ensure_ontology_schema(conn)
    await create_tenants_table(conn)
    await create_tenant(conn, tenant_id="t1", name="t1")
    return conn


@pytest.fixture
def holder() -> dict:
    return {}


@pytest.fixture
def llm() -> _ScriptedLLM:
    return _ScriptedLLM()


@pytest.fixture
def client(holder, llm):
    async def _get_conn():
        if "conn" not in holder:
            holder["conn"] = await _review_conn()
        return holder["conn"]

    app.dependency_overrides[deps.get_review_conn] = _get_conn
    # 身份用 admin：理由同 test_admin_modeling_workspace_routes.py——member 会去读
    # 测试连接里没有的 user_tenants 表；"对 member 开放"靠 tenant_scoped 无
    # require_admin_role 这一结构事实保证。
    app.dependency_overrides[deps.require_admin_session] = lambda: AdminSession(
        username="alice", role="admin", tenant_id=None, expires_at=1e18
    )
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()


_REPLY = json.dumps({
    "question": "商品分品类吗？",
    "add": {"term_types": [{"value": "商品", "rationale": "卖服装"}], "relation_types": [], "constraints": []},
    "done": False,
}, ensure_ascii=False)


def test_get_absent_returns_null(client):
    resp = client.get("/api/admin/ontology/t1/interview", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"session": None}


def test_create_starts_with_the_opening_question_without_calling_the_llm(client, llm):
    resp = client.post("/api/admin/ontology/t1/interview", headers=AUTH)
    assert resp.status_code == 200
    turns = resp.json()["session"]["state"]["turns"]
    assert turns[0]["role"] == "assistant"
    assert llm.calls == 0
    assert client.post("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 409


def test_answer_appends_turns_and_merges_skeleton(client, llm):
    llm.replies = [_REPLY]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    turns = body["session"]["state"]["turns"]
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant"]
    assert turns[-1]["text"] == "商品分品类吗？"
    assert body["session"]["state"]["skeleton"]["term_types"][0]["value"] == "商品"
    assert body["session"]["state"]["skeleton"]["term_types"][0]["from_turn"] == 1
    assert body["turn"]["question"] == "商品分品类吗？"
    assert body["turn"]["added_count"] == 1
    assert llm.calls == 1


def test_answer_with_stale_updated_at_is_409_before_calling_the_llm(client, llm):
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "x", "updated_at": "stale"},
        headers=AUTH,
    )
    assert resp.status_code == 409
    # 冲突要在调模型之前判出来——调完再发现冲突等于白等一分钟
    assert llm.calls == 0
    assert created is not None


def test_answer_when_llm_returns_garbage_keeps_the_answer_and_explains(client, llm):
    llm.replies = ["我觉得需要一个商品"]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 用户的回答不能丢；没加东西要说出来
    assert body["session"]["state"]["turns"][-1] == {"role": "user", "text": "我们卖服装"}
    assert body["turn"]["added_count"] == 0
    assert body["turn"]["note"]


def test_answer_without_session_is_404(client):
    resp = client.post("/api/admin/ontology/t1/interview/answer", json={"answer": "x", "updated_at": "y"}, headers=AUTH)
    assert resp.status_code == 404


def test_put_saves_review_decisions(client, llm):
    llm.replies = [_REPLY]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    answered = client.post(
        "/api/admin/ontology/t1/interview/answer",
        json={"answer": "我们卖服装", "updated_at": created["updated_at"]},
        headers=AUTH,
    ).json()["session"]
    state = answered["state"]
    state["skeleton"]["term_types"][0]["review"] = "accepted"
    resp = client.put(
        "/api/admin/ontology/t1/interview",
        json={"state": state, "updated_at": answered["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["session"]["state"]["skeleton"]["term_types"][0]["review"] == "accepted"


def test_put_malformed_state_is_400(client):
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.put(
        "/api/admin/ontology/t1/interview",
        json={"state": {"done": "yes"}, "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_questions_endpoint_records_needs_and_computes_missing(client, llm):
    llm.replies = [json.dumps({"needs": {"term_types": ["品类", "商品"], "relation_types": []}}, ensure_ascii=False)]
    created = client.post("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"]
    resp = client.post(
        "/api/admin/ontology/t1/interview/questions",
        json={"text": "哪个品类卖得最好？", "updated_at": created["updated_at"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"]["needs"]["term_types"] == ["品类", "商品"]
    # 骨架里什么都没有，两个都缺——由后端算，不信模型自报
    assert body["question"]["missing"] == ["品类", "商品"]
    assert body["session"]["state"]["questions"][0]["text"] == "哪个品类卖得最好？"


def test_delete_then_recreate(client):
    client.post("/api/admin/ontology/t1/interview", headers=AUTH)
    assert client.delete("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 200
    assert client.get("/api/admin/ontology/t1/interview", headers=AUTH).json()["session"] is None
    assert client.post("/api/admin/ontology/t1/interview", headers=AUTH).status_code == 200


def test_unknown_tenant_is_404(client):
    assert client.get("/api/admin/ontology/nope/interview", headers=AUTH).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**（全部 404，路由未挂）

- [ ] **Step 3: 写路由**

```python
# app/api/admin_interview_routes.py
"""智能创建（访谈式建模）的接口。只做编排：读会话、校验乐观锁、调
app/graphrag/ontology_interview.py 的纯逻辑、存回、翻译异常。

不新增写草稿的端点：前端审完骨架后走既有的 apply-preview + draft/replace，
理由同建模工作台（admin_modeling_workspace_routes.py 文件头）。

prefix 沿用 /api/admin/ontology：路由形状测试的租户前缀白名单已经覆盖它。
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api import deps
from app.api.admin_session import AdminSession
from app.api.tenant_guard import require_active_tenant_or_404
from app.graphrag.ontology_interview import ask_next, compute_missing, infer_needs, merge_additions
from app.graphrag.ontology_interview_store import (
    InterviewConflictError,
    InterviewExistsError,
    InterviewNotFoundError,
    InvalidInterviewStateError,
    create_session,
    delete_session,
    get_session,
    save_session,
)
from app.providers.registry import ProviderRegistry

router = APIRouter(prefix="/api/admin/ontology", dependencies=[Depends(deps.require_admin_session)])


class AnswerRequest(BaseModel):
    answer: str
    updated_at: str


class SaveInterviewRequest(BaseModel):
    state: dict
    updated_at: str


class QuestionRequest(BaseModel):
    text: str
    updated_at: str


def _now() -> str:
    return datetime.now().isoformat()


async def _load_or_404(review_conn: aiosqlite.Connection, tenant_id: str):
    session = await get_session(review_conn, tenant_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"租户 {tenant_id} 还没有访谈会话，先开始访谈")
    return session


def _check_lock(session, updated_at: str) -> None:
    # 乐观锁在调模型之前判：调完再发现冲突等于让用户白等一分钟。
    if session.updated_at != updated_at:
        raise HTTPException(
            status_code=409,
            detail=f"访谈在 {session.updated_at} 被 {session.updated_by} 改过，你手上这份是 {updated_at} 的。刷新后重试。",
        )


@router.get("/{tenant_id}/interview")
async def read_interview(tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    session = await get_session(review_conn, tenant_id)
    return {"session": None if session is None else session.to_dict()}


@router.post("/{tenant_id}/interview")
async def start_interview(
    tenant_id: str,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        created = await create_session(review_conn, tenant_id, actor=session.username, now=_now())
    except InterviewExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"session": created.to_dict()}


@router.post("/{tenant_id}/interview/answer")
async def answer_interview(
    tenant_id: str,
    payload: AnswerRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    current = await _load_or_404(review_conn, tenant_id)
    _check_lock(current, payload.updated_at)
    answer = payload.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="回答不能为空")

    state = dict(current.state)
    turns = [*state["turns"], {"role": "user", "text": answer}]
    result = await ask_next(
        llm_registry,
        provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        turns=turns,
        skeleton=state["skeleton"],
    )
    if result.question:
        turns.append({"role": "assistant", "text": result.question})
    state["turns"] = turns
    state["skeleton"] = merge_additions(state["skeleton"], result.added)
    state["done"] = state.get("done", False) or result.done
    try:
        saved = await save_session(
            review_conn, tenant_id, state=state, expected_updated_at=current.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    added_count = sum(len(result.added[k]) for k in ("term_types", "relation_types", "constraints"))
    return {
        "session": saved.to_dict(),
        "turn": {"question": result.question, "added_count": added_count, "dropped": result.dropped, "note": result.note},
    }


@router.put("/{tenant_id}/interview")
async def save_interview(
    tenant_id: str,
    payload: SaveInterviewRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        saved = await save_session(
            review_conn, tenant_id, state=payload.state, expected_updated_at=payload.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InvalidInterviewStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"session": saved.to_dict()}


@router.delete("/{tenant_id}/interview")
async def delete_interview(tenant_id: str, review_conn: aiosqlite.Connection = Depends(deps.get_review_conn)) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    await delete_session(review_conn, tenant_id)
    return {"deleted": True}


@router.post("/{tenant_id}/interview/questions")
async def add_interview_question(
    tenant_id: str,
    payload: QuestionRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
    session: AdminSession = Depends(deps.require_admin_session),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
) -> dict:
    await require_active_tenant_or_404(review_conn, tenant_id)
    current = await _load_or_404(review_conn, tenant_id)
    _check_lock(current, payload.updated_at)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="问题不能为空")
    needs = await infer_needs(llm_registry, provider_name=deps.DEFAULT_LLM_PROVIDER_NAME, question=text, skeleton=current.state["skeleton"])
    missing = compute_missing(needs, current.state["skeleton"])
    entry = {"text": text, "needs": needs, "missing": missing, "at": _now()}
    state = dict(current.state)
    state["questions"] = [*state.get("questions", []), entry]
    try:
        saved = await save_session(
            review_conn, tenant_id, state=state, expected_updated_at=current.updated_at,
            actor=session.username, now=_now(),
        )
    except InterviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"session": saved.to_dict(), "question": entry}
```

`deps.DEFAULT_LLM_PROVIDER_NAME` 若不是这个名字，以 `grep` 结果为准并同步改测试。

- [ ] **Step 4: 挂进 main.py**：`from app.api.admin_interview_routes import router as admin_interview_router`；`tenant_scoped.include_router(admin_modeling_workspace_router)` 之后加 `tenant_scoped.include_router(admin_interview_router)`。

- [ ] **Step 5: 跑测试**：本文件 + `tests/api/test_admin_route_shapes.py` 全绿。

- [ ] **Step 6: 变异**：把 `answer_interview` 里的 `_check_lock(current, payload.updated_at)` 挪到 `ask_next` 之后 → `test_answer_with_stale_updated_at_is_409_before_calling_the_llm` 变红（`llm.calls == 0` 不成立，且 `replies` 为空会抛 IndexError）；改回。

- [ ] **Step 7: 提交**

```bash
git add app/api/admin_interview_routes.py app/main.py tests/api/test_admin_interview_routes.py
git commit -m "feat(admin): 智能创建的访谈路由

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 前端——「本体建模」页与路由重排

**Files:**
- Create: `frontend/src/admin/ontologyModeling/OntologyModelingPage.tsx`
- Create: `frontend/src/admin/ontologyModeling/modelingPage.test.tsx`
- Modify: `frontend/src/adminRoutes.ts`、`frontend/src/App.tsx`
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`、`frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`（去掉自己的 h1）
- Modify（引用旧路由键的页面）：`frontend/src/admin/DocumentsPage.tsx:515`、`DomainCard.tsx:167`、`ErrorLogPage.tsx:473`、`SchemaEtlPage.tsx:436`、`OntologySchemaPage.tsx:333`（工作台入口 Link）
- Modify（引用旧路由键的测试）：`adminRoutes.test.ts`、`adminChrome.test.tsx`、`deleteBlockedTermType.test.tsx`、`destructiveActions.test.tsx`、`errorLog.test.tsx`、`extraFieldLabel.test.tsx`、`forwardLinks.test.tsx`、`ontologyBulkDelete.test.tsx`、`ontologyConfirm.test.tsx`、`ontologyOneOperationAtATime.test.tsx`、`ontologyVersion.test.tsx`、`modelingWorkbench/workbenchPage.test.tsx`，以及 `grep -rln "ADMIN_ROUTES.ontology\b\|ADMIN_ROUTES.guidedOntology\|PAGE_TITLES.ontology\b\|PAGE_TITLES.guidedOntology" frontend/src` 列出的其它文件

**Interfaces:**
- Produces（`adminRoutes.ts`）：
  ```ts
  ADMIN_ROUTES.ontologyModeling = '/admin/ontology/modeling'   // 替换 ontology 与 guidedOntology 两个键
  export type ModelingWay = 'template' | 'manual' | 'smart'
  export const DEFAULT_MODELING_WAY: ModelingWay = 'manual'
  export function modelingWay(way: ModelingWay): string   // `${ADMIN_ROUTES.ontologyModeling}?way=${way}`
  ```
- `OntologyModelingPage`：h1「本体建模」+ 三个 tab 按钮（`aria-pressed`，改 `?way=`，保留其它查询参数如 `version`、`from_question`）+ 按 way 渲染 `<ModelingWorkbenchPage />` / `<OntologySchemaPage />` / `<SmartCreatePanel />`（Task 5 之前先渲染一个占位 `<p>智能创建（下一任务）</p>`——**本任务结束前必须被 Task 5 替换，不算占位残留**）。

- [ ] **Step 1: 写失败的测试**

```tsx
// frontend/src/admin/ontologyModeling/modelingPage.test.tsx
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../../App'
import { SkinProvider } from '../SkinContext'
import { ConfirmProvider } from '../ConfirmContext'
import { ToastProvider } from '../ToastContext'
import { ADMIN_ROUTES, modelingWay } from '../../adminRoutes'
import { resetAdminSession } from '../useAdminAuth'

function whoami() {
  return Promise.resolve(new Response(JSON.stringify({ username: 'alice', role: 'member', tenant_id: 'demo', current_tenant_id: 'demo' }), { status: 200 }))
}

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status }))

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) return whoami()
      if (url.includes('/nav-badges')) return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 0 })
      if (url.includes('/ontology/demo/status')) return json({ confirmed: false })
      if (url.includes('/draft/checkout')) return json({ ok: true })
      if (url.includes('/term-types')) return json({ term_types: [] })
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/modeling-workspace/skills')) return json({ skills: [] })
      if (url.includes('/modeling-workspace/grounding')) return json({ status: null, grounded_term_types: [], grounded_relation_types: [], source_files: [], parse_error: null })
      if (url.includes('/modeling-workspace')) return json({ workspace: null })
      if (url.includes('/interview')) return json({ session: null })
      return new Promise(() => {})
    }),
  )
})

function Probe() {
  const { pathname, search } = useLocation()
  return <span data-testid="url">{pathname + search}</span>
}

function renderAt(path: string) {
  return render(
    <SkinProvider><ConfirmProvider><ToastProvider>
      <MemoryRouter initialEntries={[path]}><Probe /><App /></MemoryRouter>
    </ToastProvider></ConfirmProvider></SkinProvider>,
  )
}

const tabs = () => within(screen.getByRole('group', { name: '构建方式' }))

describe('本体建模页', () => {
  it('侧边栏本体创建下第一项是本体建模，旧的两项没了', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    const nav = within(await screen.findByRole('navigation', { name: '后台导航' }))
    const labels = nav.getAllByRole('link').map((a) => a.textContent?.trim())
    expect(labels.indexOf('本体建模')).toBeLessThan(labels.indexOf('本体图'))
    expect(labels).not.toContain('本体结构')
    expect(labels).not.toContain('建模工作台')
  })

  it('缺省落在手动构建——多数租户在维护已有本体，不该被扔进空工作区', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    expect(await screen.findByRole('heading', { name: '本体建模' })).toBeInTheDocument()
    expect(tabs().getByRole('button', { name: '手动构建' }).getAttribute('aria-pressed')).toBe('true')
    // 手动构建就是原来的本体结构：三个子 tab 还在
    expect(await screen.findByRole('button', { name: '实体类型' })).toBeInTheDocument()
  })

  it('切到模板构建改 URL 并渲染工作台', async () => {
    renderAt(ADMIN_ROUTES.ontologyModeling)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
    expect(await screen.findByText('空白起步')).toBeInTheDocument()
  })

  it('切 tab 保留别的查询参数（version 等）', async () => {
    renderAt(`${modelingWay('manual')}&version=confirmed`)
    await userEvent.click(await screen.findByRole('button', { name: '模板构建' }))
    expect(screen.getByTestId('url').textContent).toContain('version=confirmed')
    expect(screen.getByTestId('url').textContent).toContain('way=template')
  })

  it('版本切换器只在手动构建里', async () => {
    renderAt(modelingWay('manual'))
    expect(await screen.findByRole('group', { name: '本体版本' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '模板构建' }))
    expect(screen.queryByRole('group', { name: '本体版本' })).toBeNull()
  })

  it('旧地址重定向到对应的 tab', async () => {
    renderAt('/admin/ontology/ontology')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('manual'))
  })

  it('旧的引导建模地址重定向到模板构建', async () => {
    renderAt('/admin/ontology/guided')
    await screen.findByRole('heading', { name: '本体建模' })
    expect(screen.getByTestId('url').textContent).toBe(modelingWay('template'))
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 改路由表**（`frontend/src/adminRoutes.ts`）

- `ADMIN_ROUTES`：删掉 `ontology` 和 `guidedOntology`，在 `dashboard` 之后加 `ontologyModeling: '/admin/ontology/modeling'`（放在 `ontologyGraph` 之前）。
- 紧接着加：
  ```ts
  export type ModelingWay = 'template' | 'manual' | 'smart'
  /** 缺省是手动构建：今天多数租户在维护已有本体，首屏不该把他们扔进空工作区。 */
  export const DEFAULT_MODELING_WAY: ModelingWay = 'manual'
  export function modelingWay(way: ModelingWay): string {
    return `${ADMIN_ROUTES.ontologyModeling}?way=${way}`
  }
  ```
- `LEGACY_REDIRECTS`：
  - `'/admin/ontology': modelingWay('manual')`
  - `'/admin/model/ontology': modelingWay('manual')`
  - `'/admin/model/guided': modelingWay('template')`
  - 新增 `'/admin/ontology/ontology': modelingWay('manual')`、`'/admin/ontology/guided': modelingWay('template')`（第四代自己的旧路径）
  - 注释里补一段：第四代把本体结构与建模工作台并成了本体建模的两个 tab。
- `NAV_GROUPS` 的 `ontology` 组：`{ path: ADMIN_ROUTES.ontologyModeling, label: '本体建模', icon: Network }`、本体图、数字人（去掉 `Wand2` import 若不再用）。
- `TENANT_SCOPED_ROUTE_KEYS`：`'ontology'`、`'guidedOntology'` → `'ontologyModeling'`。
- `groupIdForPath` 逻辑不动（按路径第二段判组）。

`App.tsx`：把 `<Route path="ontology/ontology" …>` 与 `<Route path="ontology/guided" …>` 两条换成一条 `<Route path="ontology/modeling" element={<OntologyModelingPage />} />`；删掉 `OntologySchemaPage`/`ModelingWorkbenchPage` 的 import（它们改由 `OntologyModelingPage` 引入）。`LEGACY_REDIRECTS` 的渲染逻辑 `from.replace('/admin/', '')` 不动——`Navigate to` 带查询参数没问题。

- [ ] **Step 4: 写页面**

```tsx
// frontend/src/admin/ontologyModeling/OntologyModelingPage.tsx
import { useSearchParams } from 'react-router-dom'
import { DEFAULT_MODELING_WAY, type ModelingWay } from '../../adminRoutes'
import { OntologySchemaPage } from '../OntologySchemaPage'
import { ModelingWorkbenchPage } from '../modelingWorkbench/ModelingWorkbenchPage'
import { SmartCreatePanel } from './smart/SmartCreatePanel'

const WAYS: { id: ModelingWay; label: string; hint: string }[] = [
  { id: 'template', label: '模板构建', hint: '从内置领域模板起步，对齐数据表，看过差异再写入草稿' },
  { id: 'manual', label: '手动构建', hint: '逐条维护实体类型、关系类型和约束' },
  { id: 'smart', label: '智能创建', hint: '回答几个问题，让模型先猜一版骨架，再用业务问题校准' },
]

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const wayButtonClass = (active: boolean) =>
  `min-h-[40px] cursor-pointer rounded-control border border-subtle px-4 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-ink text-paper' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

function parseWay(raw: string | null): ModelingWay {
  // 未知值一律落回缺省，不报错：URL 是用户能手改的，改错了给他最安全的那页
  return raw === 'template' || raw === 'manual' || raw === 'smart' ? raw : DEFAULT_MODELING_WAY
}

/**
 * 本体建模：三种构建方式的壳。
 *
 * 只做 tab 切换与 URL 同步；三套内容各自独立（spec 决策 2），互相不 import，
 * 写草稿都走各自的路径、终点都是同一份草稿。
 *
 * tab 记在 ?way= 上而不是组件状态：链接能分享，刷新不丢；切 tab 保留别的
 * 查询参数（version、from_question），它们属于各自的 tab，不该被切换动作抹掉。
 */
export function OntologyModelingPage() {
  const [params, setParams] = useSearchParams()
  const way = parseWay(params.get('way'))

  const switchTo = (next: ModelingWay) => {
    const updated = new URLSearchParams(params)
    updated.set('way', next)
    setParams(updated, { replace: true })
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h1 className="font-mono text-xl font-semibold text-ink">本体建模</h1>
        <div role="group" aria-label="构建方式" className="flex flex-wrap gap-2">
          {WAYS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={way === item.id}
              title={item.hint}
              className={wayButtonClass(way === item.id)}
              onClick={() => switchTo(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-sm text-ink-soft">{WAYS.find((w) => w.id === way)!.hint}</p>
      </div>

      {way === 'template' && <ModelingWorkbenchPage />}
      {way === 'manual' && <OntologySchemaPage />}
      {way === 'smart' && <SmartCreatePanel />}
    </div>
  )
}
```

Task 5 之前，`./smart/SmartCreatePanel` 先建一个最小占位：

```tsx
// frontend/src/admin/ontologyModeling/smart/SmartCreatePanel.tsx（Task 5 会整个重写）
export function SmartCreatePanel() {
  return <p className="text-sm text-ink-soft">智能创建正在建设中。</p>
}
```

- [ ] **Step 5: 两个既有页面降级为 tab 内容**

- `OntologySchemaPage.tsx`：标题行里的 `<h1>…{PAGE_TITLES.ontology}</h1>` 删掉，只留 `<VersionSwitcher />`（外层那个 `flex … justify-between` 容器改成 `justify-end`）；`PAGE_TITLES` 若不再用就删 import；工作台入口那个 `<Link to={ADMIN_ROUTES.guidedOntology}>` 改成 `to={modelingWay('template')}`，文案「打开建模工作台」改「切到模板构建」。
- `ModelingWorkbenchPage.tsx`：标题行里的 `<h1>…{PAGE_TITLES.guidedOntology}</h1>` 删掉，保留 `nextStepHint` 那行和「重新起步」按钮；删 `PAGE_TITLES` import。

- [ ] **Step 6: 全仓库替换旧路由键**

规则（逐文件人工改，不要正则批量替换——每处语义不同）：
- `ADMIN_ROUTES.ontology` → `modelingWay('manual')`（`ErrorLogPage.tsx:473` 那处拼查询参数：`` `${modelingWay('manual')}&${MODEL_FROM_QUESTION_KEY}=…` ``，注意从 `?` 变 `&`）
- `ADMIN_ROUTES.guidedOntology` → `modelingWay('template')`
- 测试里 `renderAt(ADMIN_ROUTES.ontology)` → `renderAt(modelingWay('manual'))`；`renderAt(\`${ADMIN_ROUTES.ontology}?version=confirmed\`)` → `` renderAt(`${modelingWay('manual')}&version=confirmed`) ``；断言 URL 相等的地方按同样规则改（`ontologyVersion.test.tsx` 里 `url()` 的期望：`` `${modelingWay('manual')}&version=confirmed` ``；跨页那条点「本体图」后的期望是 `${ADMIN_ROUTES.ontologyGraph}?version=confirmed`——`AdminLayout` 的 NavLink 带的是当前 `search`，现在会是 `?way=manual&version=confirmed`，**把 `way` 也带去本体图页没有意义**：在 `AdminLayout.tsx` 那处 `search` 改成只保留 `version`：`const versionOnly = (() => { const p = new URLSearchParams(search); const v = p.get('version'); return v ? `?version=${v}` : '' })()`，NavLink 用 `versionOnly`）。
- `adminChrome.test.tsx:89` 那组表：`['本体结构', ADMIN_ROUTES.ontology]` → `['本体建模', ADMIN_ROUTES.ontologyModeling]`。
- `adminRoutes.test.ts`：路由表整体断言按新表改；`LEGACY_REDIRECTS` 三条期望改成 `modelingWay(...)`；`groupIdForPath(\`${ADMIN_ROUTES.ontology}/term-types\`)` 改用 `ADMIN_ROUTES.ontologyModeling`；`TENANT_SCOPED_ROUTE_KEYS` 相关断言随键名改。
- `ontologyVersion.test.tsx` 里「侧边栏里不再有一份」等断言不变；「离开建模组时版本参数不跟着走」不变。
- 页面测试里对 `heading '本体结构'` / `'建模工作台'` 的断言（若有）改成 `'本体建模'`。

改完 `grep -rn "ADMIN_ROUTES.ontology\b\|ADMIN_ROUTES.guidedOntology\|PAGE_TITLES.ontology\b\|PAGE_TITLES.guidedOntology" frontend/src` 必须为空。

- [ ] **Step 7: 跑前端全量 + tsc**：`NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2` 全绿；`npx tsc --noEmit` 干净。逐个处理失败——大多是旧路径/标题字面量。

- [ ] **Step 8: 变异**：`parseWay` 改成恒返回 `'template'` → 「缺省落在手动构建」变红；`LEGACY_REDIRECTS` 里 `'/admin/ontology/guided'` 那条删掉 → 「旧的引导建模地址重定向」变红。各自改回。

- [ ] **Step 9: 提交**（逐文件点名；文件多，`git add` 分几行写，最后 `git status --short` 确认没漏也没多）

```bash
git commit -m "refactor(admin): 本体结构与建模工作台并成「本体建模」的两个 tab，旧路径重定向

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 智能创建面板

**Files:**
- Create: `frontend/src/admin/ontologyModeling/smart/types.ts`、`interviewApi.ts`、`skeletonEdits.ts`、`SmartCreatePanel.tsx`
- Replace: Task 4 的占位 `SmartCreatePanel.tsx`
- Test: `frontend/src/admin/ontologyModeling/smart/interviewApi.test.ts`、`skeletonEdits.test.ts`、`smartCreate.test.tsx`

**Interfaces:**
- `types.ts`：`InterviewTurn{role,text}`、`SmartTermType{value, display_name, rationale, confidence:'guess', from_turn, review, extra_fields, standard_name_value_type}`、`SmartRelationType`、`SmartConstraint`、`SmartSkeleton`、`InterviewQuestion{text, needs, missing, at}`、`InterviewState{turns, skeleton, questions, done}`、`InterviewSession{tenant_id, state, updated_at, updated_by}`、`TurnReport{question, added_count, dropped, note}`
- `interviewApi.ts`：`fetchInterview` / `startInterview` / `answerInterview(tenantId, token, answer, updatedAt) -> {session, turn}` / `saveInterview` / `deleteInterview` / `addQuestion(tenantId, token, text, updatedAt) -> {session, question}`；409 → `InterviewConflictError`；路径 `/api/admin/ontology/{tenant}/interview…`
- `skeletonEdits.ts`（纯函数，非法输入原样返回同引用）：`setReview(state, kind:'term'|'relation'|'constraint', key, review)`、`addMissingAsTerm(state, name, questionText)`（`provenance` 概念这里用 `rationale: '来自问题：…'`，`confidence:'guess'`，`review:'pending'`，`from_turn` 取最后一轮）、`addMissingAsRelation(...)`
- `projectSkeleton(state): DraftPayload`——只投影 `accepted`，约束三者都 accepted 才带；**在本目录自己写一份**，不 import 模板构建的 `projectToDraft`（决策 2）。`DraftPayload` 类型也自己声明（形状与 `/draft/replace` 一致）。

**面板布局**（一个组件，三块）：
1. **访谈**（左）：对话流（assistant 灰底、user 右对齐）；底部输入框 + 「回答」按钮（busy 禁用）；`done` 后显示「访谈已结束」并隐藏输入框；「结束访谈」按钮（PUT `done: true`）；每轮回复后的 `turn.note` / `turn.dropped` 显示在对话流末尾（`role="status"`）。没有会话时显示「开始访谈」按钮（POST）。
2. **骨架**（右）：三节（实体类型 / 关系类型 / 约束），每条：名字、标「这是猜的」、`rationale` 文字、「出自第 N 轮」按钮（点击滚动到对应对话气泡，`id="turn-N"`）、接受/拒绝按钮（吃 busy）。
3. **问题清单**（下）：输入框 + 「加一条」（POST questions）；每条显示 `needs` 与 `missing`，`missing` 里每个名字带「加进骨架」按钮（实体名走 `addMissingAsTerm`，全大写下划线的走 `addMissingAsRelation`）；文案「确认本体后可以拿这些问题去问答页验收」。
4. **写入**（底部）：「看看会改什么」（既有 `previewApply`——从 `../../modelingWorkbench/workspaceApi` import **仅这一个只读函数**？不行，决策 2 要求不 import；**自己在 `interviewApi.ts` 里再封装一次 `previewDraft(tenantId, token, payload)`**，路径同 `/modeling-workspace/apply-preview`）+「写入草稿」（走 `/draft/replace`，`etl_mapping: null`；有删除项 `useConfirm`；没看过 diff 先算一次——与模板构建同一条规则）+「重新开始」（DELETE，`useConfirm`）。

- [ ] **Step 1: 写失败的测试**

`skeletonEdits.test.ts`（纯函数）：
- `setReview` 改对应元素、不改入参、不存在原样返回；
- `addMissingAsTerm` 加 `{value, rationale:'来自问题：哪个品类…', confidence:'guess', review:'pending', from_turn: turns.length-1, extra_fields:[], standard_name_value_type:'string'}`，重名原样返回；
- `projectSkeleton`：只 accepted；约束三者都 accepted 才带；`extra_fields` 原样。

`interviewApi.test.ts`：路径转义、`answerInterview` body `{answer, updated_at}`、409 → `InterviewConflictError`、`addQuestion` body。

`smartCreate.test.tsx`（渲染整个 App 于 `modelingWay('smart')`，fetch 桩照 `workbenchPage.test.tsx` 的写法，`session` 变量可切换）：
1. 没有会话时显示「开始访谈」；点了之后 POST，出现开场问题气泡。
2. 输入回答点「回答」→ POST `/interview/answer` body 含 answer 与 updated_at → 桩返回新会话（骨架里有「商品」，`turn.added_count: 1`）→ 骨架区出现「商品」「这是猜的」和 rationale。
3. 桩返回 `turn.note` 时对话流末尾显示那句话（`role="status"`）。
4. 点「接受 商品」→ PUT body 里 `skeleton.term_types[0].review === 'accepted'`。
5. 问题清单：输入「哪个品类卖得最好」点「加一条」→ POST `/interview/questions` → 显示 `missing: 品类` 和「把 品类 加进骨架」→ 点它 → PUT body 的 `skeleton.term_types` 里多了 `品类`。
6. 写入草稿：骨架里「商品」accepted，点「写入草稿」→ 先 POST `apply-preview`（桩返回 `removed_term_types: ['手工加的']`）→ 确认框出现「继续写入」→ 点它 → POST `/draft/replace`，body 的 `term_types[0].value === '商品'` 且 `etl_mapping === null`。
7. 点「取消」不写。

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**——`types.ts` / `interviewApi.ts`（照 `modelingWorkbench/workspaceApi.ts` 的 `readOrThrow` 写法）/ `skeletonEdits.ts` / `SmartCreatePanel.tsx`。面板的状态：`session`、`draft`（回答输入）、`questionDraft`、`diff`、`busy`、`error`、`turnReport`；所有写操作走一个 `persist(next: InterviewState)`（PUT，带 `session.updated_at`，成功后替换 `session`、`setDiff(null)`）。写草稿的 `handleApply` 逻辑照 `ModelingWorkbenchPage.handleApply`（自己写一份，不 import）。按钮全部吃 `busy`；输入框有 label（`回答` / `业务问题`）；错误提示用 `rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink`。

- [ ] **Step 4: 跑测试**：`NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/ontologyModeling --maxWorkers=2` 全绿，`npx tsc --noEmit` 干净。

- [ ] **Step 5: 变异**：(a) `projectSkeleton` 的约束过滤去掉"关系 accepted"条件 → 对应纯函数测试变红；(b) `handleApply` 里"没有 diff 先算一次"删掉 → 测试 6 变红（确认框不出现）；(c) `addMissingAsTerm` 重名检查删掉 → 重名测试变红。各自改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/ontologyModeling/smart/types.ts frontend/src/admin/ontologyModeling/smart/interviewApi.ts frontend/src/admin/ontologyModeling/smart/skeletonEdits.ts frontend/src/admin/ontologyModeling/smart/SmartCreatePanel.tsx frontend/src/admin/ontologyModeling/smart/interviewApi.test.ts frontend/src/admin/ontologyModeling/smart/skeletonEdits.test.ts frontend/src/admin/ontologyModeling/smart/smartCreate.test.tsx
git commit -m "feat(admin): 智能创建——访谈出骨架、问题清单校准、审阅后写入草稿

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 全量回归与收尾

- [ ] 前端全量 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2` + `npx tsc --noEmit`；后端全量 `PYTHONIOENCODING=utf-8 python -u -m pytest tests/ -q`。全绿。
- [ ] `grep -rn "本体结构\|建模工作台\|引导建模" frontend/src --include=*.tsx --include=*.ts | grep -v test | grep -v "^.*//"`：界面可见文案里不该再有旧名字（注释里提及不算；`OntologySchemaPage` 内部"本体结构"若作为语义描述出现要改成"手动构建"）。
- [ ] 人工验收（用户在场）：侧边栏「本体建模」→ 缺省手动构建、版本切换在页头 → 切模板构建看工作台 → 切智能创建：开始访谈、答两轮、看骨架长出来并标"这是猜的"、接受两条、加一条业务问题看缺口、把缺的加进去、写入草稿（看 diff、确认）→ 手动构建里看到写进去的实体。

## 完成判据
- 侧边栏「本体创建」：本体建模 / 本体图 / 数字人。
- `/admin/ontology/modeling?way=manual|template|smart` 三个 tab 可切、URL 可分享；旧地址全部重定向。
- 智能创建端到端可用：访谈 → 骨架 → 问题清单 → 写草稿；LLM 失败时界面有说明。
- 前后端全量全绿。
