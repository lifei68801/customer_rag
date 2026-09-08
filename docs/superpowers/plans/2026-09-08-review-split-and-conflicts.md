# 审核拆分与属性值冲突 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关系审核按理由拆成四个分页各带自己的修复动作；属性值冲突不再静默覆盖。

**Architecture:** 拆分**不需要任何新后端能力**——`graph_review_queue.reason` 这一列早就在库里，四种理由由 `app/graphrag/normalization.py` 在四处写入。属性值冲突是新建：在 `upsert_term_with_node_key` 覆盖之前比对，值不同就记一条冲突并**保留先写的值不动**。

**Tech Stack:** FastAPI · aiosqlite · React 18 + TypeScript · vitest

**Spec:** `docs/superpowers/specs/2026-09-08-interaction-redesign-design.md`

**Depends on:** `docs/superpowers/plans/2026-09-08-nav-restructure-and-dashboard.md`（审核页要落在新导航的「数据审核」组里，占位页在那里建好了）

## Global Constraints

同 `2026-09-08-multi-persona-foundation.md` 的十二条，逐字适用。额外一条：

13. **不确定的时候不擅自改数据。** 属性值冲突的处理原则是「记下来、保留先写的、等人来定」，不是「猜一个」。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `app/graphrag/attribute_conflicts.py`（新） | `attribute_conflicts` 表 DDL 与读写 |
| `app/graphrag/terms_store.py`（改） | `upsert_term_with_node_key` 覆盖前比对 |
| `app/graphrag/review_queue.py`（改） | `list_pending_reviews` 支持按 reason 过滤与计数 |
| `app/api/admin_review_routes.py`（改） | 关系审核列表加 `reason` 参数；新增两个就地修复端点 |
| `app/api/admin_conflicts_routes.py`（新） | 属性冲突的列表与决议 |
| `frontend/src/admin/GraphReviewsPage.tsx`（改） | 四个分页 |
| `frontend/src/admin/AttributeConflictsPage.tsx`（新） | 冲突审核页 |
| `frontend/src/admin/DirtyEdgesPage.tsx`（新） | 脏边与孤儿数据 |

---

## Task 1: 审核队列按理由过滤与计数

**Files:**
- Modify: `app/graphrag/review_queue.py:205-228`（`list_pending_reviews`）
- Modify: `app/graphrag/review_queue.py:268-276`（`count_pending_reviews`）
- Test: `tests/graphrag/test_review_queue.py`（追加）

**Interfaces:**
- Produces:
  - `list_pending_reviews(conn, *, tenant_id, limit=None, offset=0, reasons: list[str] | None = None)`
  - `count_pending_reviews(conn, *, tenant_id, reasons: list[str] | None = None) -> int`
  - `async def count_pending_by_reason(conn, *, tenant_id: str) -> dict[str, int]`——分页角标用

**四种 reason 的准确值**（来自 `app/graphrag/normalization.py`，不要凭记忆写）：
`fuzzy_match_needs_confirmation` · `subject_unresolved` · `object_unresolved` ·
`not_in_confirmed_ontology` · `invalid_relation_type`

**四个分页与 reason 的映射**：

| 分页 key | reasons |
|---|---|
| `fuzzy` | `fuzzy_match_needs_confirmation` |
| `unresolved` | `subject_unresolved`, `object_unresolved` |
| `out_of_ontology` | `not_in_confirmed_ontology` |
| `bad_type` | `invalid_relation_type` |

- [ ] **Step 1: （已核对，可直接照写）**

五个取值已跟源码逐字比对过，全部吻合：

| 出处 | reason |
|---|---|
| `normalization.py:186` | `fuzzy_match_needs_confirmation` |
| `normalization.py:202` | `subject_unresolved` / `object_unresolved`（同一个三元表达式的两个分支） |
| `normalization.py:232` | `not_in_confirmed_ontology` |
| `normalization.py:279` | `invalid_relation_type` |

**注意第 202 行是 `reason = "..."` 赋值形式，不是 `reason="..."` 关键字参数形式**
——只 grep 后者会漏掉这两个，而漏掉它们就意味着「一端对不上」那一页永远是空的。
Task 2 里那条覆盖性用例（`test_every_reason_the_pipeline_writes_lands_in_exactly_one_tab`）
正是为了让将来新增 reason 时这件事必红。

- [ ] **Step 2: 写失败测试**

在 `tests/graphrag/test_review_queue.py` 追加：

```python
def test_list_pending_reviews_filters_by_reason():
    """按 reason 过滤。批次里必须同时有命中和不命中的两种——
    全都命中的话，「不过滤」的实现也能变绿。"""

    async def run():
        conn = await _conn()
        try:
            await enqueue_for_review(conn, tenant_id="t1", reason="subject_unresolved", ...)
            await enqueue_for_review(conn, tenant_id="t1", reason="invalid_relation_type", ...)
            rows = await list_pending_reviews(
                conn, tenant_id="t1", reasons=["subject_unresolved"]
            )
            assert [r["reason"] for r in rows] == ["subject_unresolved"]
        finally:
            await conn.close()

    asyncio.run(run())


def test_a_page_can_ask_for_two_reasons_at_once():
    """「一端对不上」这一页要同时收 subject_unresolved 和 object_unresolved：
    它们是同一个问题的两侧，审核员做的事一模一样。"""


def test_reasons_none_still_returns_everything():
    """不传 reasons 时行为不变——review_cli.py 等既有调用方不传这个参数，
    它们的行为一个字都不该变。"""


def test_filtering_is_still_scoped_to_the_tenant():
    """加了 reason 过滤不能把租户过滤挤掉。两个租户各有一条同样 reason 的，
    断言只拿到自己那条。"""


def test_count_pending_by_reason_covers_every_reason_present():
    """分页角标要显示每一页各有几条。少算一种的话，那一页的角标恒为 0，
    审核员永远不会点进去——而里面积着待办。"""

    async def run():
        conn = await _conn()
        try:
            # 三种 reason，条数各不相同：2 / 1 / 3
            counts = await count_pending_by_reason(conn, tenant_id="t1")
            assert counts == {
                "subject_unresolved": 2,
                "invalid_relation_type": 1,
                "not_in_confirmed_ontology": 3,
            }
        finally:
            await conn.close()

    asyncio.run(run())


def test_count_pending_by_reason_ignores_resolved_rows():
    """已处理的不该计入角标。计入的话角标永远降不下去，
    审核员点进去发现是空的。"""
```

补全成可运行用例（`enqueue_for_review` 的真实签名见 `app/graphrag/review_queue.py:152`，
先读一遍）。

- [ ] **Step 3: 跑测试确认它红 → 写实现 → 绿**

```python
async def list_pending_reviews(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    limit: int | None = None,
    offset: int = 0,
    reasons: list[str] | None = None,
) -> list[dict[str, Any]]:
    """limit=None（默认）返回该租户全部待审核记录，保持 review_cli.py 等
    既有调用方不传这些参数时的行为不变；管理后台分页时显式传入。

    reasons=None（默认）不按理由过滤。传具体值时只返回这几种理由的——
    审核页拆成四个分页之后，每一页问的是它自己那一两种理由
    （见 docs/superpowers/specs/2026-09-08-interaction-redesign-design.md §6）。
    """
    conn.row_factory = aiosqlite.Row
    where = "status = 'pending' AND tenant_id = ?"
    params: list[Any] = [tenant_id]
    if reasons:
        placeholders = ",".join("?" for _ in reasons)
        where += f" AND reason IN ({placeholders})"
        params.extend(reasons)
    params.extend([limit if limit is not None else -1, offset])
    cursor = await conn.execute(
        "SELECT review_id, subject_candidate, object_candidate, relation_type, "
        "reason, suggested_subject_standard_name, suggested_object_standard_name, "
        "source, evidence, created_at, subject_type_candidate, object_type_candidate "
        f"FROM graph_review_queue WHERE {where} ORDER BY review_id LIMIT ? OFFSET ?",
        tuple(params),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def count_pending_by_reason(
    conn: aiosqlite.Connection, *, tenant_id: str
) -> dict[str, int]:
    """每种理由各有几条待审。四个分页的角标用。

    只数 pending：已处理的计进去的话，角标永远降不下去，审核员点进去
    发现是空的。
    """
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT reason, COUNT(*) AS n FROM graph_review_queue "
        "WHERE status = 'pending' AND tenant_id = ? GROUP BY reason",
        (tenant_id,),
    )
    return {row["reason"]: row["n"] for row in await cursor.fetchall()}
```

`count_pending_reviews` 同样加 `reasons` 参数，实现同构。

- [ ] **Step 4: 变异 + 提交**

```bash
# 变异 A：reasons 非空时也不拼 IN 子句
#   预期红：test_list_pending_reviews_filters_by_reason
# 变异 B：拼 reason 过滤时把 tenant_id 那一条去掉
#   预期红：test_filtering_is_still_scoped_to_the_tenant
# 变异 C：count_pending_by_reason 去掉 status = 'pending'
#   预期红：test_count_pending_by_reason_ignores_resolved_rows
```

```bash
git add app/graphrag/review_queue.py tests/graphrag/test_review_queue.py
git commit -m "feat(review): 待审队列能按理由过滤和分组计数"
```

---

## Task 2: 审核页拆成四个分页

**Files:**
- Modify: `app/api/admin_review_routes.py`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`
- Test: `tests/api/test_admin_review_routes.py`（追加）
- Test: `frontend/src/admin/reviewTabs.test.tsx`（新）

**Interfaces:**
- Produces:
  - `GET /api/admin/{tenant_id}/reviews?tab=fuzzy|unresolved|out_of_ontology|bad_type` → 只返回该 tab 的条目
  - `GET /api/admin/{tenant_id}/reviews/counts` → `{"fuzzy": 2, "unresolved": 5, "out_of_ontology": 0, "bad_type": 1}`

**tab → reasons 的映射写在后端**，不是前端。前端传 tab 名、后端翻译成 reasons：
映射写在前端的话，reason 字符串会同时存在于前后端两处，改一个忘一个就是一个永远
空着的分页。

- [ ] **Step 1: 写失败测试（后端）**

```python
def test_each_tab_returns_only_its_own_reasons(review_conn):
    """五种 reason 各造一条，逐个 tab 断言它拿到的正是自己那几种。
    这是整个拆分的核心断言。"""


def test_the_unresolved_tab_collects_both_sides(review_conn):
    """subject_unresolved 和 object_unresolved 都归「一端对不上」。
    只收一种的话，另一半待办会消失在界面上——队列里还在，没有任何页面列它。"""


def test_an_unknown_tab_name_is_a_400_not_an_empty_list(review_conn):
    """拼错 tab 名返回 400 而不是空列表。返回空的话，前端拼错一个字母
    就得到一个永远空着的分页，而它看起来完全正常。"""
    assert _get_reviews(review_conn, tab="拼错了").status_code == 400


def test_counts_endpoint_reports_every_tab_including_the_empty_ones(review_conn):
    """空的那一页角标是 0，不是这个 key 不存在。缺 key 的话前端得写
    `?? 0` 兜底，而那会把「后端没算这一页」和「这一页真的是 0」混成一件事。"""
    body = _get_counts(review_conn).json()
    assert sorted(body) == ["bad_type", "fuzzy", "out_of_ontology", "unresolved"]


def test_every_reason_the_pipeline_writes_lands_in_exactly_one_tab(review_conn):
    """管线写入的每一种 reason 都必须落进恰好一个 tab。

    这条用例防的是最阴的那个 bug：normalization.py 将来加一种新 reason，
    没人记得更新映射，那批待办就永远不出现在任何页面上——队列里积着，
    界面上一片清净。
    """
    from app.api.admin_review_routes import TAB_REASONS
    covered = [r for reasons in TAB_REASONS.values() for r in reasons]
    assert sorted(covered) == sorted(set(covered)), "同一个 reason 落进了两个 tab"
    # 管线里真实出现的 reason 全集，从源码里数出来而不是手写一遍
    import re, pathlib
    source = pathlib.Path("app/graphrag/normalization.py").read_text(encoding="utf-8")
    written = set(re.findall(r'reason="([a-z_]+)"', source))
    # reason 变量赋值那一处（subject_unresolved / object_unresolved）单独补上
    written |= set(re.findall(r'reason = "([a-z_]+)"', source))
    written |= {"subject_unresolved", "object_unresolved"}
    assert written <= set(covered), f"这些 reason 没有归属的分页：{written - set(covered)}"
```

最后一条是整个任务里最值钱的用例。

- [ ] **Step 2: 跑测试确认它红 → 写实现 → 绿**

```python
#: 分页 → 它收哪几种 reason。
#:
#: 映射写在后端而不是前端：写在前端的话，reason 字符串会同时存在于前后端
#: 两处，改一个忘一个就是一个永远空着的分页，而它看起来完全正常。
#:
#: 拆成四页不是为了好看：审核员面对这四类时要做的事完全不同——
#: subject_unresolved 要他去新建实体，not_in_confirmed_ontology 要他去决定
#: 放宽本体。此前它们共用同一对「批准/驳回」按钮。
TAB_REASONS: dict[str, list[str]] = {
    "fuzzy": ["fuzzy_match_needs_confirmation"],
    "unresolved": ["subject_unresolved", "object_unresolved"],
    "out_of_ontology": ["not_in_confirmed_ontology"],
    "bad_type": ["invalid_relation_type"],
}
```

路由里 `tab` 不在 `TAB_REASONS` 时 `raise HTTPException(400, ...)`，
detail 里列出合法值。

- [ ] **Step 3: 前端四个分页**

`GraphReviewsPage.tsx` 顶部加一排 tab，每个带角标（来自 counts 端点）。
切 tab 换 `?tab=` 参数重新拉。

每一页的**修复动作不同**（spec §6）：

- `fuzzy`：「确认建议名」「换一个」「驳回」
- `unresolved`：「就地新建实体」「驳回」——新建成功后自动批准这一条
- `out_of_ontology`：「就地加白名单组合」「驳回」——加成功后自动批准
- `bad_type`：一个已确认关系类型的下拉 +「用这个类型批准」「驳回」

本任务先做**分页与角标**，四种修复动作里 `fuzzy` 和 `bad_type` 用既有的批准/驳回，
`unresolved` 和 `out_of_ontology` 的就地修复放 Task 3。

- [ ] **Step 4: 前端测试**

```tsx
it('四个分页都在，角标是各自的条数', async () => {
  // 角标必须四个值互不相同，否则「所有角标都显示同一个总数」的实现也能变绿。
})

it('切分页会带上 tab 参数重新拉', async () => {})

it('角标为 0 的分页照常可点，不是禁用', async () => {
  // 禁用的话，审核员没法确认「这一类真的清空了」——他只能看到一个灰的按钮。
})

it('某一页拉取失败只影响那一页', async () => {})
```

- [ ] **Step 5: 变异 + 提交**

```bash
# 变异 A：TAB_REASONS 里把 object_unresolved 删掉
#   预期红：test_the_unresolved_tab_collects_both_sides
#           以及 test_every_reason_..._lands_in_exactly_one_tab
# 变异 B：未知 tab 返回空列表而不是 400
#   预期红：test_an_unknown_tab_name_is_a_400_not_an_empty_list
# 变异 C：counts 端点只返回非零的那几个 key
#   预期红：test_counts_endpoint_reports_every_tab_including_the_empty_ones
```

```bash
git add app/api/admin_review_routes.py tests/api/test_admin_review_routes.py frontend/src/admin/GraphReviewsPage.tsx frontend/src/admin/reviewTabs.test.tsx
git commit -m "feat(review): 审核队列按理由拆成四个分页，映射写在后端一处"
```

---

## Task 3: 两个就地修复动作

**Files:**
- Modify: `app/api/admin_review_routes.py`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`
- Test: `tests/api/test_admin_review_routes.py`（追加）

**Interfaces:**
- Produces:
  - `POST /api/admin/{tenant_id}/reviews/{review_id}/create-missing-term` `{standard_name, term_type, side: "subject"|"object"}` → 建实体并批准这条审核
  - `POST /api/admin/{tenant_id}/reviews/{review_id}/allow-combination` → 把这条审核的类型组合加进白名单并批准

- [ ] **Step 1: 写失败测试**

```python
def test_create_missing_term_creates_it_and_approves_the_review(review_conn):
    """两件事一起做。分成两步的话，审核员建完实体还得回来手动批准，
    而中间任何中断都会留下一个「实体已建、审核还挂着」的状态。"""


def test_create_missing_term_rolls_back_the_review_if_the_graph_write_fails(review_conn):
    """建实体成功但写图失败时，这条审核不能标成已批准。
    标了的话，这条关系永远不会进图，而队列里也看不到它了。"""


def test_create_missing_term_refuses_a_type_not_in_the_confirmed_ontology(review_conn):
    """新建实体的类型必须是已确认本体里的。放行的话，审核这个动作
    自己就制造出了一个孤儿类型——而它绕过了本体那一层的全部校验。"""
    assert _create_missing(review_conn, term_type="不存在的类型").status_code == 400


def test_allow_combination_adds_it_and_approves(review_conn):
    """加白名单 + 批准。加的是这条审核自己那个组合，不是别的。"""


def test_allow_combination_refuses_when_the_types_are_unknown(review_conn):
    """subject_type_candidate 为空时（管线没识别出类型）不能加白名单——
    加进去的会是一个带空类型的组合，它匹配不上任何东西。"""


def test_both_actions_are_scoped_to_the_tenant(review_conn):
    """两个端点都必须走 require_tenant_access。它们都会写数据，
    比列表端点更敏感。"""
    assert _create_missing(review_conn, tenant="别人的", role="member").status_code == 403
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

实现要点：两个端点都**先校验再写**，写图失败时不标记审核为已批准
（照抄 `admin_terms_routes.py` 里「先写编辑再删节点」那段的顺序论证）。

- [ ] **Step 5: 前端接上**

`unresolved` 分页的每一行加一个内联表单：实体名（预填 `subject_candidate` 或
`object_candidate`）+ 类型下拉（已确认类型）+「新建并批准」。

`out_of_ontology` 分页的每一行加一个按钮：「允许 `产品 -HAS_FLAVOR-> 口味` 并批准」——
**按钮上写出具体组合**，不是「加白名单」。审核员要知道自己在放宽什么。

- [ ] **Step 6: 变异 + 提交**

```bash
# 变异 A：写图失败时仍标记审核为已批准
#   预期红：test_create_missing_term_rolls_back_the_review_if_the_graph_write_fails
# 变异 B：去掉类型在已确认本体里的校验
#   预期红：test_create_missing_term_refuses_a_type_not_in_the_confirmed_ontology
# 变异 C：两个端点的 require_tenant_access 换成 require_admin_session
#   预期红：test_both_actions_are_scoped_to_the_tenant
```

```bash
git add app/api/admin_review_routes.py tests/api/test_admin_review_routes.py frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(review): 一端对不上和超出本体两页能就地修复，不用跳到别处再回来"
```

---

## Task 4: 属性值冲突表

**Files:**
- Create: `app/graphrag/attribute_conflicts.py`
- Modify: `app/main.py`
- Test: `tests/graphrag/test_attribute_conflicts.py`

**Interfaces:**
- Produces:
  - `async def ensure_attribute_conflicts_schema(conn) -> None`
  - `async def record_conflict(conn, *, tenant_id, node_key, field, kept_value, kept_source, incoming_value, incoming_source) -> None`
  - `async def list_conflicts(conn, *, tenant_id, limit=None, offset=0) -> list[dict[str, Any]]`
  - `async def count_conflicts(conn, *, tenant_id) -> int`
  - `async def resolve_conflict(conn, *, tenant_id, conflict_id: int, chosen_value: str, resolved_by: str) -> str`——返回被选中的值，供调用方写回 terms

DDL：

```sql
CREATE TABLE IF NOT EXISTS attribute_conflicts (
    conflict_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    field           TEXT NOT NULL,
    kept_value      TEXT NOT NULL,
    kept_source     TEXT NOT NULL,
    incoming_value  TEXT NOT NULL,
    incoming_source TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'resolved')),
    resolved_value  TEXT,
    resolved_by     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_attribute_conflicts_pending
    ON attribute_conflicts (tenant_id, status, conflict_id);
```

- [ ] **Step 1: 写失败测试**

```python
def test_record_and_list_a_conflict():
    """一条冲突要能说清四件事：哪个实体、哪个属性、留下的是什么值来自哪、
    被挡下的是什么值来自哪。少任何一样，审核员都没法判断该选哪个。"""


def test_the_same_conflict_recorded_twice_does_not_pile_up():
    """同一个 (实体, 属性) 反复导入不该堆出一百条待审。
    堆起来的话，审核员面对的是同一个问题的一百个副本。

    重复的定义是 (tenant_id, node_key, field) 且 status='pending'——
    incoming_value 变了要更新那一条，不是新增。
    """


def test_conflicts_are_scoped_to_the_tenant():
    """两个租户各记一条，断言各自只看到自己的。"""


def test_resolve_marks_it_and_records_who():
    """决议要记谁决的。记不下来的话，这张表跟本项目里那 15 张一样，
    回答不了「谁改的」。"""


def test_resolving_twice_is_refused():
    """已决议的不能再决议一次。放行的话，第二个人的选择会悄悄覆盖第一个人的，
    而两人都以为自己的生效了。"""


def test_a_resolved_conflict_leaves_the_pending_count():
    """决议之后 count_conflicts 减一。不减的话看板角标永远降不下去。"""


def test_resolve_accepts_a_value_that_is_neither_of_the_two():
    """审核员可以手填第三个值——两个来源都错是可能的，
    强制二选一等于逼他选一个已知是错的。"""
```

- [ ] **Step 2 – Step 4: 红 → 实现 → 绿**

- [ ] **Step 5: 变异 + 提交**

```bash
# 变异 A：record_conflict 每次都 INSERT（不去重）
#   预期红：test_the_same_conflict_recorded_twice_does_not_pile_up
# 变异 B：resolve 不检查当前 status
#   预期红：test_resolving_twice_is_refused
# 变异 C：resolve 强制 chosen_value 必须是两个值之一
#   预期红：test_resolve_accepts_a_value_that_is_neither_of_the_two
```

```bash
git add app/graphrag/attribute_conflicts.py tests/graphrag/test_attribute_conflicts.py app/main.py
git commit -m "feat(review): 属性值冲突落一张表，同一个实体同一个属性不堆重复"
```

---

## Task 5: 让 upsert 不再静默覆盖（本计划的核心）

**Files:**
- Modify: `app/graphrag/terms_store.py:995-1030`（`upsert_term_with_node_key`）
- Test: `tests/graphrag/test_terms_store.py`（追加）

**Interfaces:**
- Produces: `upsert_term_with_node_key(..., conflict_conn: aiosqlite.Connection | None = None, incoming_source: str = "unknown")`
  ——`conflict_conn` 为 None 时行为**完全不变**（既有调用方一个字不用改）

**今天的行为**（`terms_store.py:1018-1020`）：

```sql
INSERT INTO terms (...) VALUES (...)
ON CONFLICT (tenant_id, node_key) DO UPDATE SET ...
```

表格 A 说售价 39、表格 B 说 45，**后跑的赢，没有任何人知道发生过冲突**。
这是 spec §1 点名的「今天最大的静默失败」。

**改后的行为**：`conflict_conn` 不为 None 时，覆盖之前先读一遍现有的
`extra_properties`，逐字段比对：值不同的字段**保留旧值**，并记一条冲突。

- [ ] **Step 1: 写失败测试**

```python
def test_a_differing_attribute_value_is_recorded_and_the_old_value_is_kept():
    """这是整个计划的核心断言。

    表格 A 写 39，表格 B 写 45。改后：库里还是 39，冲突表里多一条。
    「保留旧值」不是随便选的——不确定的时候不擅自改数据。改成新值的话，
    这次改动只是把静默覆盖换了个方向，没解决任何问题。
    """


def test_an_identical_value_records_no_conflict():
    """同一个值反复导入不是冲突。记成冲突的话，每天跑一次 ETL
    就会积出一屏「39 和 39 冲突了」。"""


def test_a_new_field_is_written_normally():
    """旧记录没有这个字段时直接写入，不算冲突。
    算成冲突的话，补充属性这个正常动作会被当成问题。"""


def test_conflicts_are_recorded_per_field_not_per_row():
    """一行里两个字段都变了要记两条。合并成一条的话，审核员只能整行
    选 A 或选 B——而正确答案可能是「售价用 A、产地用 B」。"""


def test_standard_name_change_is_not_treated_as_an_attribute_conflict():
    """改名走的是 term_edits 那条路（人工编辑层），不是属性冲突。
    混进来的话，ETL 每次跑都会跟人工改的名字冲突一次。"""


def test_without_conflict_conn_the_behaviour_is_byte_for_byte_the_old_one():
    """conflict_conn=None 时完全走老路径——覆盖，不记冲突。

    既有调用方（review 路径、手工新建）不该因为这次改动而行为变化。
    这条用例是那些调用方的安全网。
    """


def test_the_conflict_carries_both_sources():
    """审核页要显示「值 A 来自 商品表.xlsx」。来源丢了的话，
    审核员看到两个数字但不知道该信哪个。"""
```

- [ ] **Step 2: 跑测试确认它红**

- [ ] **Step 3: 写实现**

```python
async def upsert_term_with_node_key(
    conn: aiosqlite.Connection,
    *,
    tenant_id: str,
    node_key: str,
    standard_name: str,
    aliases: list[str],
    term_type: str,
    extra_properties: dict[str, Any],
    conflict_conn: aiosqlite.Connection | None = None,
    incoming_source: str = "unknown",
) -> None:
    """按 node_key 写入或更新一条术语。

    conflict_conn 不为 None 时，覆盖之前逐字段比对已有的 extra_properties：
    值不同的字段**保留旧值**，并往 attribute_conflicts 记一条。

    保留旧值而不是取新值，是因为不确定的时候不该擅自改数据——取新值只是
    把静默覆盖换了个方向。真正的决定交给审核页（spec §6）。

    conflict_conn 为 None 时行为跟 2026-09-08 之前逐字相同：直接覆盖，
    不记冲突。既有调用方（人工新建、审核批准）不传这个参数，它们的行为
    一个字都没变。

    ON CONFLICT ... DO UPDATE SET 故意不包含 source = excluded.source——
    这一条是既有行为，保持不动。
    """
```

实现骨架：

```python
    if conflict_conn is not None:
        existing = await get_term_by_node_key(conn, tenant_id, node_key)
        if existing is not None:
            kept = dict(existing.extra_properties)
            for field, incoming in extra_properties.items():
                old = kept.get(field)
                if old is None or old == incoming:
                    kept[field] = incoming
                    continue
                await record_conflict(
                    conflict_conn,
                    tenant_id=tenant_id, node_key=node_key, field=field,
                    kept_value=str(old), kept_source=existing.source,
                    incoming_value=str(incoming), incoming_source=incoming_source,
                )
                # 冲突字段保留旧值：kept[field] 不动
            extra_properties = kept
    # 以下走既有的 INSERT ... ON CONFLICT，一字不改
```

- [ ] **Step 4: 跑测试确认它绿 + 跑 terms_store 全部用例**

```
.venv/Scripts/python.exe -m pytest tests/graphrag/test_terms_store.py -q -p no:cacheprovider
```
Expected: 全绿。**既有用例一条都不能改**——`conflict_conn` 默认 None，
它们的行为按设计不变。有红的说明默认路径被改了，回去修实现而不是修用例。

- [ ] **Step 5: 变异**

```bash
# 变异 A：冲突时取新值（kept[field] = incoming）
#   预期红：test_a_differing_attribute_value_is_recorded_and_the_old_value_is_kept
# 变异 B：值相同也记冲突
#   预期红：test_an_identical_value_records_no_conflict
# 变异 C：整行记一条冲突而不是逐字段
#   预期红：test_conflicts_are_recorded_per_field_not_per_row
# 变异 D：conflict_conn 为 None 时也走比对路径
#   预期红：test_without_conflict_conn_the_behaviour_is_byte_for_byte_the_old_one
```

- [ ] **Step 6: 让 ETL 传上 conflict_conn**

修改 `app/graphrag/schema_etl.py:186-190` 那处调用，传入 `conflict_conn=conn`
和 `incoming_source=mapping.source_file`。

**只改 ETL 这一处**。文档抽取和人工新建两条路径不传——文档抽取的属性本来就
是低置信度的推断，把它跟表格导入的权威数值放进同一个冲突队列会淹掉真正的冲突；
人工新建就是人的决定，跟自己冲突没有意义。

- [ ] **Step 7: 补一条 ETL 端到端用例**

```python
def test_running_two_sheets_with_different_prices_records_a_conflict():
    """两张表给同一个商品不同的售价。跑完之后：库里是第一张表的值，
    冲突表里有一条，第二张表的值和来源都在那条里。

    这条用例是整个计划面向用户的那个承诺——它红了就说明静默覆盖回来了。
    """
```

- [ ] **Step 8: 提交**

```bash
git add app/graphrag/terms_store.py app/graphrag/schema_etl.py tests/graphrag/test_terms_store.py tests/graphrag/test_schema_etl.py
git commit -m "fix(etl): 属性值不一致不再静默覆盖，记一条冲突并保留先写的值"
```

---

## Task 6: 冲突审核页

**Files:**
- Create: `app/api/admin_conflicts_routes.py`
- Create: `frontend/src/admin/AttributeConflictsPage.tsx`
- Modify: `frontend/src/App.tsx`（换掉阶段三建的占位页）
- Test: `tests/api/test_admin_conflicts_routes.py`
- Test: `frontend/src/admin/attributeConflicts.test.tsx`

**Interfaces:**
- Produces:
  - `GET /api/admin/{tenant_id}/conflicts` → 分页列表
  - `POST /api/admin/{tenant_id}/conflicts/{conflict_id}/resolve` `{value: str}` → 写回 terms 并标记已决议

- [ ] **Step 1: 写失败测试**

后端：

```python
def test_resolving_writes_the_chosen_value_back_into_terms(conflicts_conn):
    """决议不是只改冲突表——选中的值要真的进 terms。不写回的话，
    审核员选完发现实体上的值没变，而系统说「已解决」。"""


def test_resolving_also_syncs_the_graph(conflicts_conn):
    """图谱侧也要跟着改。只改 SQLite 的话，问答拿到的还是旧值——
    而审核页显示这条已经处理完了。"""


def test_a_failed_graph_sync_does_not_mark_the_conflict_resolved(conflicts_conn):
    """写图失败时冲突不能标成已决议。标了的话它从队列里消失，
    而图上还是旧值，没有任何地方能再发现它。"""


def test_resolve_records_the_real_operator(conflicts_conn):
    """记 session.username，不是写死的常量。"""


def test_conflicts_are_scoped_to_the_tenant(conflicts_conn):
    assert _resolve(conflicts_conn, tenant="别人的", role="member").status_code == 403
```

前端：

```tsx
it('一条冲突把两个值和各自来源都摆出来', async () => {
  // 「39（来自 商品表.xlsx 第 88 行）」和「45（来自 价格库.csv 第 12 行）」。
  // 只给两个数字的话，审核员没有任何依据判断该信哪个。
})

it('三个动作都在：选 A、选 B、手填', async () => {
  // 两个来源都错是可能的，强制二选一等于逼他选一个已知是错的。
})

it('决议之后这条从列表里消失', async () => {})

it('决议失败时这条留在原地并说出原因', async () => {
  // 消失的话审核员以为处理完了。
})
```

- [ ] **Step 2 – Step 5: 红 → 实现 → 绿 → 变异**

```bash
# 变异 A：resolve 只更新冲突表，不写回 terms
#   预期红：test_resolving_writes_the_chosen_value_back_into_terms
# 变异 B：写图失败时仍标记已决议
#   预期红：test_a_failed_graph_sync_does_not_mark_the_conflict_resolved
# 变异 C：前端去掉「手填」那个入口
#   预期红：test「三个动作都在」
```

- [ ] **Step 6: 重启后端 + 提交**

```bash
git add app/api/admin_conflicts_routes.py tests/api/test_admin_conflicts_routes.py frontend/src/admin/AttributeConflictsPage.tsx frontend/src/admin/attributeConflicts.test.tsx frontend/src/App.tsx
git commit -m "feat(admin): 属性冲突审核页，两个来源都摆出来还能手填第三个值"
```

---

## Task 7: 脏边与孤儿数据页

**Files:**
- Create: `frontend/src/admin/DirtyEdgesPage.tsx`
- Modify: `app/api/admin_terms_routes.py`（加一个全局列举端点）
- Test: 后端 + 前端各一份

**Interfaces:**
- Consumes: 既有的 `list_inconsistent_relation_edges(*, tenant_id, node_key)`——它是**按实体**的
- Produces:
  - `GET /api/admin/{tenant_id}/dirty-edges` → 整租户的脏边清单（新图谱查询）
  - 前端页面，复用阶段四之前已有的批量删除组件

**今天的状况**：脏边的列举接口只在实体详情页里，**没有全局视图**——
你得先知道是哪个实体才能看到它的脏边。而脏边的特点恰恰是没人知道它们在哪。

- [ ] **Step 1 – Step 6**：照 Task 6 的形状（红 → 实现 → 绿 → 变异 → 提交）。

图谱侧新查询要点：口径跟 `_LIST_INCONSISTENT_RELATION_EDGES_QUERY` 逐字一致
（边的 tenant_id 为 null、或与两端节点对不上、或两端节点分属不同租户），
只是去掉 `node_key` 这个锚点、改成扫整个租户，并加上一个结果上限
（默认 500，超出时**说出来**：「已截断，列了 500 条，共 3271 条」）。

关键用例：

```python
def test_the_listing_says_when_it_truncated():
    """超过上限时必须说出来。默默少列的话，运维会以为脏边只有 500 条。"""
```

---

## Task 8: 收尾验证

- [ ] **Step 1: 后端全量 + 前端全量 + 类型检查**

- [ ] **Step 2: 重启前后端并手工走一遍**

八项：

1. 造两张表格，同一个商品售价一个 39 一个 45，依次导入
2. 导完打开属性冲突页 → **有一条冲突**，两个值和两个来源都在
3. 打开实体明细看那个商品 → 售价还是 **39**（先写的那个）
4. 在冲突页选 45 → 实体明细和问答里都变成 45
5. 再导一次同样的表格 → 冲突页**不再多一条**（去重）
6. 打开关系审核 → 四个分页，角标各不相同
7. 在「一端对不上」页就地新建一个实体 → 这条审核自动批准并消失
8. 在「超出本体范围」页点「允许 X -R-> Y 并批准」→ 白名单里多了这个组合

第 1–5 项是这个计划面向用户的全部承诺，**不能跳过**。

---

## Self-Review

**1. Spec coverage**

| Spec 要求 | 落在哪 |
|---|---|
| D4 关系队列拆四页 | Task 1 + Task 2 |
| §6 四页各带修复动作 | Task 2（fuzzy / bad_type）+ Task 3（unresolved / out_of_ontology） |
| D4 属性值冲突 | Task 4 + Task 5 + Task 6 |
| §6 保留先写的值不动 | Task 5 Step 3 |
| §6 三个动作：选 A、选 B、手填 | Task 4 的 `resolve_conflict` + Task 6 前端 |
| D4 脏边与孤儿 | Task 7 |
| D4 实体消歧 | **与 Task 2 的 `fuzzy` 页合并**——`fuzzy_match_needs_confirmation` 就是消歧失败那一类，`resolve_term_or_candidates`（`app/graphrag/ontology.py:89`）已经把「没找到」和「有歧义」分开了。单独再开一页会让同一个问题出现在两个地方。 |
| D4 明确不做低置信度审核 | 无任务，spec 已说明理由 |

**2. Placeholder scan**：Task 3 / Task 6 / Task 7 的用例给的是骨架 + 理由，
需按仓库既有夹具补全。Task 7 整体压缩成「照 Task 6 的形状」+ 关键要点 + 一条核心用例
——它跟 Task 6 是同构的 CRUD 页，逐行重写只会产生两份需要同步的描述。

**3. Type consistency**

- `reasons: list[str] | None`：Task 1 定义，Task 2 消费。
- `TAB_REASONS: dict[str, list[str]]`：Task 2 定义，Task 2 的用例直接 import 它做覆盖性断言。
- `record_conflict(...)` 八个关键字参数：Task 4 定义，Task 5 消费，字段名与 DDL 一致。
- `resolve_conflict(...) -> str`：Task 4 定义，Task 6 消费。

---

## 已知的执行前不确定项

只剩两条，且都需要读源码判断而不只是查找：

1. **`get_term_by_node_key` 是否存在、返回什么**（Task 5 Step 3）：先 grep，没有就用 `get_term_merged_by_node_key`——但要注意合并视图会带上人工编辑，比对时该用哪一个需要读一遍 `term_merge` 的语义再定。
2. **`admin_review_routes.py` 的实际文件名**：`GraphReviewsPage` 对应的后端路由文件名先 grep 确认。

已在写计划时核对完毕的：**五个 reason 取值逐字吻合**（`normalization.py` 第 186 / 202 / 232 / 279 行，
其中 `subject_unresolved` / `object_unresolved` 是第 202 行那个三元表达式的两个分支），
Task 2 的 `TAB_REASONS` 映射表可以直接照写；`enqueue_for_review` 的签名
（`review_queue.py:152-166`，十三个关键字参数，`source` 和 `tenant_id` 必填无默认值）；
`count_pending_reviews(conn, *, tenant_id) -> int`（`review_queue.py:268`）。
