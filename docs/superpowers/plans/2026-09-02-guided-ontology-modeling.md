# 引导式本体建模 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让新用户传一张真实业务表，平台扫描列、推荐一整套本体草案，用户审阅调整后一键写入草稿——把「凭空想出实体类型/关系类型/约束」这个门槛换成「审阅一份基于你自己数据的草案」。

**Architecture:** 主体是前端：三个纯函数模块（扫描 → 判定 → 生成草案）串成一条流水线，外面套一个三步向导页。后端只加一个端点——原子替换整份草稿，因为现有 API 只能逐个写，14 个请求中途失败会留下残缺草稿，而 `checkout_draft` **不会**清空它（它只在「还没检出过」时才复制），用户没有干净的重来方式。

**Tech Stack:** React + TypeScript + vitest（前端），FastAPI + aiosqlite + pytest（后端）。文件解析复用已有的 SheetJS（xlsx）与手写 CSV 解析。

**Spec:** `docs/superpowers/specs/2026-09-02-guided-ontology-modeling-design.md`

## Global Constraints

- **统计量在前端全量扫描，数据不出用户的机器。** 不上传、不采样。
- **distinct 集合必须带上限**（`DISTINCT_LIMIT = 1000`）：超过就停止收集并标记为高基数。判定只需要知道低基数还是高基数。
- **绝不采样前 N 行。** 前 N 行不是随机样本——订单表按时间排序的话，前 1000 行可能只有 3 个州，基数估计严重偏低，把本该是实体的列判成属性，而这不会报错。
- **低基数列默认判为实体类型。** 少建是静默错误（那类问题就是答不出来，不报错），多建看得见（实体列表里就有）。
- **「约束」这个词不出现在引导 UI 里。** 用户画出的结构就是 `term_type_relation_allowlist`，由代码生成。
- **`allow_chain_query` 不暴露给用户**，一律写 `true`。
- **`example_phrase` 由代码生成**，不问用户。
- 关系类型名必须匹配 `^[A-Z][A-Z0-9_]{0,63}$`（`app/graphrag/ontology_relations.py:24`）。
- 属性字段名必须匹配 `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`（`app/graphrag/ontology_categories.py:26`）。
- 属性值类型只能是 `string` / `number` / `integer` / `number[]`（`ontology_categories.py:23`）——**没有日期类型**。
- 产出交给现有的 `buildConfigYaml()`，不新写 YAML 生成。
- 引导产出 **draft**，走现有 draft/confirmed 生命周期，不新建状态。
- **每写完一条否定式断言（「X 不应该出现」「不该被判成 Y」），必须故意破坏实现确认它变红，然后恢复。**
- 前端：`cd frontend && npm test`、`npm run typecheck`、`npm run build`。后端：`pytest`。
- Windows 下跑 Python 输出中文需要 `PYTHONIOENCODING=utf-8`；`pytest` 跑完会卡在解释器退出阶段（aiosqlite 非 daemon 线程），用 `timeout N ... > 日志文件` 的方式跑，不要用管道。

---

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `app/graphrag/ontology_lifecycle.py` | **改**：加 `replace_draft()`，一个事务里替换三张草稿表 |
| `app/api/admin_ontology_routes.py` | **改**：加 `POST /{tenant_id}/draft/replace` |
| `frontend/src/admin/guidedOntology/types.ts` | 新建：整条流水线的类型 |
| `frontend/src/admin/guidedOntology/columnStats.ts` | 新建：扫描文件 → 每列统计量（纯函数 + 一个读文件的入口） |
| `frontend/src/admin/guidedOntology/columnRoles.ts` | 新建：统计量 → 角色判定（纯函数） |
| `frontend/src/admin/guidedOntology/draftProposal.ts` | 新建：角色 + 用户选择 → 本体草案 + ETL 映射（纯函数） |
| `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx` | 新建：三步向导的外壳与状态 |
| `frontend/src/admin/guidedOntology/ProposalReview.tsx` | 新建：审阅视图（实体/属性选择、层级、命名、未使用列） |
| `frontend/src/adminRoutes.ts` | **改**：加 `guidedOntology` 路由 |
| `frontend/src/App.tsx` | **改**：挂路由 |
| `frontend/src/admin/OntologySchemaPage.tsx` | **改**：加引导入口 |

前四个纯逻辑模块可以完全用单元测试覆盖，不需要渲染。**先做它们**——UI 只是这条流水线的外壳。

---

### Task 1: 后端原子替换草稿

**Files:**
- Modify: `app/graphrag/ontology_lifecycle.py`（加 `replace_draft`）
- Modify: `app/api/admin_ontology_routes.py`（加端点）
- Test: `tests/graphrag/test_ontology_lifecycle.py`（既有文件，追加）
- Test: `tests/api/test_admin_ontology_routes.py`（既有文件，追加）

**Interfaces:**
- Consumes: 无
- Produces:
  - `replace_draft(conn, tenant_id, *, term_types, relation_types, constraints) -> None`
    - `term_types`: `list[dict]`，键 `value` / `extra_fields` / `standard_name_value_type`
    - `relation_types`: `list[dict]`，键 `relation_type` / `example_phrase` / `description` / `allow_chain_query`
    - `constraints`: `list[dict]`，键 `subject_term_type` / `relation_type` / `object_term_type`
  - `POST /api/admin/ontology/{tenant_id}/draft/replace`，请求体同上三个键，响应 `{"replaced": true}`

- [ ] **Step 1: 写失败的存储层测试**

追加到 `tests/graphrag/test_ontology_lifecycle.py`。**该文件没有 `conn` fixture**，
每个用例自己 `conn = await _conn()`（见文件顶部），下面照这个形态写：

```python
async def test_replace_draft_swaps_the_whole_draft():
    conn = await _conn()
    """整份替换，不是增量合并。

    引导每次提交的都是一份完整草案（用户改了一条边就重新提交整份），
    增量合并的话，用户删掉的那个实体类型会留在草稿里——界面上没有了，
    库里还在，确认时又冒出来。
    """
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="旧类型")

    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
    )

    values = [t["value"] for t in await list_term_types(conn, "t1", status="draft")]
    assert values == ["订单号"]


async def test_replace_draft_is_atomic():
    conn = await _conn()
    """中途失败必须整份回滚。

    留下半份草稿是最糟的形态：用户看到一个残缺的本体，而 checkout_draft
    **不会**清空它（它只在"还没检出过"时才从已确认版复制），所以用户没有
    干净的重来方式，只能去三个 tab 逐个删。
    """
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="原有类型")

    with pytest.raises(Exception):
        await replace_draft(
            conn,
            "t1",
            term_types=[
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"},
            ],
            # 引用了不存在的实体类型，必然失败
            relation_types=[],
            constraints=[
                {"subject_term_type": "订单号", "relation_type": "NOPE", "object_term_type": "幽灵"}
            ],
        )

    values = [t["value"] for t in await list_term_types(conn, "t1", status="draft")]
    assert values == ["原有类型"], "失败后草稿必须保持原样"


async def test_replace_draft_does_not_touch_confirmed():
    conn = await _conn()
    """已确认版本是只读快照。替换草稿动到它，等于绕过了确认这道关。"""
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="已确认的")
    await confirm_ontology(conn, "t1")

    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "新的", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
    )

    confirmed = [t["value"] for t in await list_term_types(conn, "t1", status="confirmed")]
    assert confirmed == ["已确认的"]


async def test_replace_draft_marks_the_tenant_as_checked_out():
    conn = await _conn()
    """写过草稿就意味着已检出。

    不标记的话，下一次 checkout_draft 会以为"还没检出过"，把已确认版本
    复制回来盖在引导刚写的草稿上——用户点完引导，回头一看草稿变回了旧的。
    """
    await checkout_draft(conn, "t1")
    await create_term_type(conn, tenant_id="t1", value="已确认的")
    await confirm_ontology(conn, "t1")

    await replace_draft(
        conn,
        "t1",
        term_types=[{"value": "引导建的", "extra_fields": [], "standard_name_value_type": "string"}],
        relation_types=[],
        constraints=[],
    )
    await checkout_draft(conn, "t1")

    values = [t["value"] for t in await list_term_types(conn, "t1", status="draft")]
    assert values == ["引导建的"]
```

`create_term_type` / `list_term_types` / `checkout_draft` / `confirm_ontology` 该文件顶部
已经导入了，只需在 `ontology_lifecycle` 那组导入里加上 `replace_draft`。`pytest` 已在
顶部导入。

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 timeout 120 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_lifecycle.py -q > /tmp/t.log 2>&1; tail -3 /tmp/t.log`
Expected: FAIL — `ImportError: cannot import name 'replace_draft'`

- [ ] **Step 3: 实现 `replace_draft`**

加到 `app/graphrag/ontology_lifecycle.py`：

```python
async def replace_draft(
    conn: aiosqlite.Connection,
    tenant_id: str,
    *,
    term_types: list[dict],
    relation_types: list[dict],
    constraints: list[dict],
) -> None:
    """把该租户的三张草稿表整份替换成提交的内容。一个事务，要么全成要么全不动。

    为什么需要这个函数，而不是让调用方逐个调 create_term_type /
    create_relation_type / add_allowed_combination：

    引导一次要写入十几个对象，逐个调的话中途失败会留下半份草稿。而
    checkout_draft **不会**清空草稿（它只在"还没检出过"时才从已确认版
    复制），所以用户没有干净的重来方式，只能去三个 tab 逐个删。

    整份替换而不是增量合并：引导每次提交的都是一份完整草案（用户改一条边
    就重新提交整份）。增量合并的话，用户删掉的那个实体类型会留在草稿里
    ——界面上没有了，库里还在，确认时又冒出来。

    校验顺序是先类型后约束：约束引用实体类型和关系类型，反过来先插约束会
    撞上"引用的类型不存在"，而那时前面的类型已经写进去了。
    """
    await _ensure_checkout_state_schema(conn)
    try:
        await conn.execute("BEGIN")
        for table in (
            "ontology_term_types",
            "tenant_relation_types",
            "term_type_relation_allowlist",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ? AND status = 'draft'", (tenant_id,)
            )

        for term_type in term_types:
            _validate_term_type_value(term_type["value"])
            extra_fields = _normalize_extra_fields(term_type.get("extra_fields", []))
            _validate_standard_name_value_type(term_type["standard_name_value_type"])
            await conn.execute(
                "INSERT INTO ontology_term_types"
                " (tenant_id, value, extra_fields, standard_name_value_type, status)"
                " VALUES (?, ?, ?, ?, 'draft')",
                (
                    tenant_id,
                    term_type["value"],
                    json.dumps(extra_fields, ensure_ascii=False),
                    term_type["standard_name_value_type"],
                ),
            )

        for relation_type in relation_types:
            _validate_relation_type(relation_type["relation_type"])
            await conn.execute(
                "INSERT INTO tenant_relation_types"
                " (tenant_id, relation_type, example_phrase, description, allow_chain_query,"
                "  source, status) VALUES (?, ?, ?, ?, ?, 'custom', 'draft')",
                (
                    tenant_id,
                    relation_type["relation_type"],
                    relation_type.get("example_phrase", ""),
                    relation_type.get("description", ""),
                    1 if relation_type.get("allow_chain_query", True) else 0,
                ),
            )

        declared_types = {t["value"] for t in term_types}
        declared_relations = {r["relation_type"] for r in relation_types}
        for constraint in constraints:
            # 引用检查放在这里而不是靠外键：SQLite 默认不强制外键，靠它等于
            # 没检查。引用不存在的类型会让 ETL 在跑批时才炸，那时已经晚了。
            for key, pool, label in (
                ("subject_term_type", declared_types, "实体类型"),
                ("object_term_type", declared_types, "实体类型"),
                ("relation_type", declared_relations, "关系类型"),
            ):
                if constraint[key] not in pool:
                    raise UnknownCategoryError(
                        f"约束引用了未声明的{label}：{constraint[key]}"
                    )
            await conn.execute(
                "INSERT INTO term_type_relation_allowlist"
                " (tenant_id, subject_term_type, relation_type, object_term_type, status)"
                " VALUES (?, ?, ?, ?, 'draft')",
                (
                    tenant_id,
                    constraint["subject_term_type"],
                    constraint["relation_type"],
                    constraint["object_term_type"],
                ),
            )

        # 写过草稿就意味着已检出。不标记的话，下一次 checkout_draft 会以为
        # "还没检出过"，把已确认版本复制回来盖在引导刚写的草稿上。
        await conn.execute(
            "INSERT OR IGNORE INTO ontology_draft_checkout_state (tenant_id) VALUES (?)",
            (tenant_id,),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
```

顶部按需补导入：`json`，以及从 `ontology_categories` / `ontology_relations` / `ontology_constraints` 导入校验函数与 `UnknownCategoryError`。**先读那三个文件确认校验函数的真实名字**——上面用的名字是示意，实际名字以源码为准；若它们是模块私有（下划线开头）且不宜跨模块引用，就在本函数里内联同样的校验，并注释说明为什么重复。

- [ ] **Step 4: 跑测试确认通过**

Run: `PYTHONIOENCODING=utf-8 timeout 120 .venv/Scripts/python.exe -m pytest tests/graphrag/test_ontology_lifecycle.py -q > /tmp/t.log 2>&1; tail -3 /tmp/t.log`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认三条断言各自会红**

逐个改、逐个跑、逐个恢复：

1. 把 `except Exception: await conn.rollback(); raise` 改成 `except Exception: raise`（不回滚）→ `test_replace_draft_is_atomic` 应 FAIL
2. 把三个 DELETE 里的 `AND status = 'draft'` 去掉 → `test_replace_draft_does_not_touch_confirmed` 应 FAIL
3. 把 `INSERT OR IGNORE INTO ontology_draft_checkout_state` 那句删掉 → `test_replace_draft_marks_the_tenant_as_checked_out` 应 FAIL

- [ ] **Step 6: 加 API 端点**

`app/api/admin_ontology_routes.py`，参照该文件既有端点的写法：

```python
class DraftTermTypePayload(BaseModel):
    value: str
    extra_fields: list[dict] = []
    standard_name_value_type: str = "string"


class DraftRelationTypePayload(BaseModel):
    relation_type: str
    example_phrase: str = ""
    description: str = ""
    allow_chain_query: bool = True


class DraftConstraintPayload(BaseModel):
    subject_term_type: str
    relation_type: str
    object_term_type: str


class ReplaceDraftRequest(BaseModel):
    term_types: list[DraftTermTypePayload]
    relation_types: list[DraftRelationTypePayload]
    constraints: list[DraftConstraintPayload]


@router.post("/{tenant_id}/draft/replace")
async def replace_ontology_draft(
    tenant_id: str,
    payload: ReplaceDraftRequest,
    review_conn: aiosqlite.Connection = Depends(deps.get_review_conn),
) -> dict[str, bool]:
    """整份替换草稿。引导页用它一次写入整套本体。

    没有对应的"增量"端点：引导每次提交的都是完整草案，增量合并会让用户
    删掉的东西留在库里。
    """
    await require_active_tenant_or_404(review_conn, tenant_id)
    try:
        await replace_draft(
            review_conn,
            tenant_id,
            term_types=[t.model_dump() for t in payload.term_types],
            relation_types=[r.model_dump() for r in payload.relation_types],
            constraints=[c.model_dump() for c in payload.constraints],
        )
    except (UnknownCategoryError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"replaced": True}
```

- [ ] **Step 7: 写端点测试**

追加到 `tests/api/test_admin_ontology_routes.py`（沿用该文件既有的 client / conn fixture 形态）：

```python
def test_replace_draft_writes_the_whole_ontology(client, ...):
    """一次请求写入整套本体。"""
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"},
                {"value": "产品", "extra_fields": [], "standard_name_value_type": "string"},
            ],
            "relation_types": [
                {"relation_type": "CONTAINS", "example_phrase": "订单 CONTAINS 产品"}
            ],
            "constraints": [
                {"subject_term_type": "订单号", "relation_type": "CONTAINS", "object_term_type": "产品"}
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 200

    listed = client.get(
        "/api/admin/ontology/t1/term-types?status=draft", headers={"Authorization": "Bearer x"}
    ).json()
    assert {t["value"] for t in listed["term_types"]} == {"订单号", "产品"}


def test_replace_draft_rejects_constraint_referencing_undeclared_type(client, ...):
    """引用未声明的类型必须 400，不能静静写进去。

    写进去的话，ETL 跑批时才会炸——那时用户已经在等结果了，而错误信息
    指向的是 ETL，不是本体。
    """
    response = client.post(
        "/api/admin/ontology/t1/draft/replace",
        json={
            "term_types": [
                {"value": "订单号", "extra_fields": [], "standard_name_value_type": "string"}
            ],
            "relation_types": [{"relation_type": "CONTAINS", "example_phrase": ""}],
            "constraints": [
                {"subject_term_type": "订单号", "relation_type": "CONTAINS", "object_term_type": "幽灵"}
            ],
        },
        headers={"Authorization": "Bearer x"},
    )
    assert response.status_code == 400
```

fixture 与鉴权 override 沿用该文件既有的 `_fake_admin_session` 写法。

- [ ] **Step 8: 跑后端全量**

Run: `PYTHONIOENCODING=utf-8 timeout 400 .venv/Scripts/python.exe -m pytest -q > /tmp/full.log 2>&1; grep -E "passed|failed" /tmp/full.log | tail -1`
Expected: 全部 passed

- [ ] **Step 9: 提交**

```bash
git add app/graphrag/ontology_lifecycle.py app/api/admin_ontology_routes.py tests/graphrag/test_ontology_lifecycle.py tests/api/test_admin_ontology_routes.py
git commit -m "feat: 原子替换本体草稿的端点

引导一次要写入十几个对象。逐个调现有 API 的话，中途失败会留下半份草稿
——而 checkout_draft 不会清空它（只在"还没检出过"时才从已确认版复制），
用户没有干净的重来方式，只能去三个 tab 逐个删。

整份替换而不是增量合并：引导每次提交的都是完整草案，增量合并会让用户
删掉的实体类型留在库里——界面上没有了，确认时又冒出来。

写入后标记 checkout 状态，否则下次 checkout_draft 会以为还没检出过，把
已确认版本复制回来盖在引导刚写的草稿上。"
```

---

### Task 2: 列统计扫描

**Files:**
- Create: `frontend/src/admin/guidedOntology/types.ts`
- Create: `frontend/src/admin/guidedOntology/columnStats.ts`
- Test: `frontend/src/admin/guidedOntology/columnStats.test.ts`

**Interfaces:**
- Consumes: 无
- Produces:
  - `DISTINCT_LIMIT = 1000`
  - `interface ColumnStats { name: string; nonEmptyCount: number; distinctCount: number; distinctCapped: boolean; samples: string[]; inferredType: 'number' | 'integer' | 'date' | 'string' }`
  - `accumulateRow(acc: StatsAccumulator, row: string[]): void`
  - `createAccumulator(columns: string[]): StatsAccumulator`
  - `finalizeStats(acc: StatsAccumulator): ColumnStats[]`
  - `scanTableFile(file: File): Promise<ColumnStats[]>`

- [ ] **Step 1: 写失败的测试**

`frontend/src/admin/guidedOntology/columnStats.test.ts`：

```ts
import { describe, expect, it } from 'vitest'
import {
  DISTINCT_LIMIT,
  accumulateRow,
  createAccumulator,
  finalizeStats,
} from './columnStats'

/** 造一份统计结果：列名 + 每行的值。 */
function statsOf(columns: string[], rows: string[][]) {
  const acc = createAccumulator(columns)
  for (const row of rows) accumulateRow(acc, row)
  return finalizeStats(acc)
}

const byName = (columns: string[], rows: string[][]) =>
  Object.fromEntries(statsOf(columns, rows).map((s) => [s.name, s]))

describe('基数统计', () => {
  it('数出不同值的个数', () => {
    const stats = byName(['state'], [['CA'], ['TX'], ['CA'], ['NY']])
    expect(stats.state.distinctCount).toBe(3)
    expect(stats.state.distinctCapped).toBe(false)
  })

  it('超过上限就封顶并打标，不再继续收集', () => {
    // 不封顶的话，一张百万行的表会把每列的所有值都留在内存里。判定只需要
    // 知道"低基数还是高基数"，不需要精确数字。
    const rows = Array.from({ length: DISTINCT_LIMIT + 500 }, (_, i) => [`v${i}`])
    const stats = byName(['id'], rows)
    expect(stats.id.distinctCapped).toBe(true)
    expect(stats.id.distinctCount).toBe(DISTINCT_LIMIT)
  })

  it('空值不算进非空计数，也不算进不同值', () => {
    // 把空值当成一个"值"的话，一列 90% 为空的数据会被算成低基数，
    // 判成实体类型——而它其实是个稀疏的可选字段。
    const stats = byName(['note'], [['a'], [''], ['b'], ['']])
    expect(stats.note.nonEmptyCount).toBe(2)
    expect(stats.note.distinctCount).toBe(2)
  })
})

describe('类型推断', () => {
  it('全是整数 → integer', () => {
    expect(byName(['n'], [['1'], ['2'], ['30']]).n.inferredType).toBe('integer')
  })

  it('有小数 → number', () => {
    expect(byName(['n'], [['1.5'], ['2']]).n.inferredType).toBe('number')
  })

  it('一个非数字就不是数值列', () => {
    // 混进一个 "N/A" 就整列当字符串——按数值处理会在 ETL 时静默丢掉那一行，
    // 或者把 N/A 变成 0。
    expect(byName(['n'], [['1'], ['2'], ['N/A']]).n.inferredType).toBe('string')
  })

  it('日期格式 → date', () => {
    expect(byName(['d'], [['2026-01-15'], ['2026-02-03']]).d.inferredType).toBe('date')
  })

  it('纯数字的订单号不该被当成数值列', () => {
    // 高基数的整数列几乎总是标识而不是度量。把它判成 number 会让它被
    // 归进属性，整个实体就没了。
    const rows = Array.from({ length: 200 }, (_, i) => [`${100000 + i}`])
    expect(byName(['order_id'], rows).order_id.inferredType).toBe('string')
  })
})

describe('样例值', () => {
  it('保留前几个不同值给用户看', () => {
    // 判断"这列该不该是实体"时，用户要看到真实的值。只给一个数字
    // （"50 个不同值"）他判断不了。
    const stats = byName(['state'], [['CA'], ['TX'], ['CA'], ['NY'], ['FL']])
    expect(stats.state.samples.slice(0, 3)).toEqual(['CA', 'TX', 'NY'])
  })
})

describe('列数与行长不一致', () => {
  it('短行按空值补齐，不报错', () => {
    // CSV 里尾部空列常被省略。报错会让整个引导卡在第一步。
    const stats = byName(['a', 'b'], [['1', '2'], ['3']])
    expect(stats.b.nonEmptyCount).toBe(1)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/columnStats.test.ts`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现 types.ts**

```ts
export type InferredType = 'number' | 'integer' | 'date' | 'string'

export interface ColumnStats {
  name: string
  /** 非空值的行数。分母用它，不用总行数——90% 为空的列不该被当成低基数。 */
  nonEmptyCount: number
  /** 不同值的个数。封顶后等于 DISTINCT_LIMIT，见 distinctCapped。 */
  distinctCount: number
  /** 是否已封顶。封顶意味着"至少这么多"，不是精确值。 */
  distinctCapped: boolean
  /** 前几个不同值，给用户看。只给数字他判断不了。 */
  samples: string[]
  inferredType: InferredType
}
```

- [ ] **Step 4: 实现 columnStats.ts**

```ts
import type { ColumnStats, InferredType } from './types'

/**
 * 每列最多收集这么多不同值，超过就封顶。
 *
 * 判定只需要知道"低基数还是高基数"，不需要精确数字。不封顶的话，一张
 * 百万行的表会把每列的所有值都留在内存里。
 */
export const DISTINCT_LIMIT = 1000

/** 样例值给用户看，不需要多。 */
const SAMPLE_LIMIT = 5

/**
 * 高于这个基数的整数列一律当字符串。
 *
 * 高基数的整数几乎总是标识（订单号、SKU），不是度量。判成 number 会让它
 * 被归进属性，那个实体类型就整个没了——而这不会报错。
 */
const NUMERIC_IDENTIFIER_THRESHOLD = 50

interface ColumnAccumulator {
  name: string
  nonEmptyCount: number
  distinct: Set<string>
  capped: boolean
  sawNonNumeric: boolean
  sawFraction: boolean
  sawNonDate: boolean
  sawAnyValue: boolean
}

export interface StatsAccumulator {
  columns: ColumnAccumulator[]
}

const DATE_PATTERN = /^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T].*)?$/

export function createAccumulator(columns: string[]): StatsAccumulator {
  return {
    columns: columns.map((name) => ({
      name,
      nonEmptyCount: 0,
      distinct: new Set<string>(),
      capped: false,
      sawNonNumeric: false,
      sawFraction: false,
      sawNonDate: false,
      sawAnyValue: false,
    })),
  }
}

export function accumulateRow(acc: StatsAccumulator, row: string[]): void {
  acc.columns.forEach((column, index) => {
    // 短行按空值补齐：CSV 里尾部空列常被省略，报错会让引导卡在第一步。
    const raw = (row[index] ?? '').trim()
    if (raw === '') return
    column.nonEmptyCount += 1
    column.sawAnyValue = true
    if (!column.capped) {
      column.distinct.add(raw)
      if (column.distinct.size >= DISTINCT_LIMIT) column.capped = true
    }
    if (!/^-?\d+(\.\d+)?$/.test(raw)) column.sawNonNumeric = true
    else if (raw.includes('.')) column.sawFraction = true
    if (!DATE_PATTERN.test(raw)) column.sawNonDate = true
  })
}

function inferType(column: ColumnAccumulator): InferredType {
  if (!column.sawAnyValue) return 'string'
  if (!column.sawNonDate) return 'date'
  if (column.sawNonNumeric) return 'string'
  // 高基数的整数列是标识，不是度量。
  if (!column.sawFraction && (column.capped || column.distinct.size > NUMERIC_IDENTIFIER_THRESHOLD)) {
    return 'string'
  }
  return column.sawFraction ? 'number' : 'integer'
}

export function finalizeStats(acc: StatsAccumulator): ColumnStats[] {
  return acc.columns.map((column) => ({
    name: column.name,
    nonEmptyCount: column.nonEmptyCount,
    distinctCount: column.capped ? DISTINCT_LIMIT : column.distinct.size,
    distinctCapped: column.capped,
    samples: [...column.distinct].slice(0, SAMPLE_LIMIT),
    inferredType: inferType(column),
  }))
}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/columnStats.test.ts`
Expected: 全部 passed

- [ ] **Step 6: 破坏实现，确认四条断言各自会红**

逐个改、逐个跑、逐个恢复：

1. 把 `if (raw === '') return` 删掉 → 「空值不算进非空计数」应 FAIL
2. 把 `NUMERIC_IDENTIFIER_THRESHOLD` 判断整段删掉 → 「纯数字的订单号不该被当成数值列」应 FAIL
3. 把 `if (column.distinct.size >= DISTINCT_LIMIT) column.capped = true` 删掉 → 「超过上限就封顶」应 FAIL
4. 把 `if (!/^-?\d+(\.\d+)?$/.test(raw)) column.sawNonNumeric = true` 删掉 → 「一个非数字就不是数值列」应 FAIL

- [ ] **Step 7: 加读文件的入口**

在 `columnStats.ts` 末尾追加。**必须先读 `frontend/src/admin/schemaEtlConfigBuilder/tableHeader.ts`**，复用它的扩展名分流与 CSV 引号解析逻辑，不要另写一份解析：

```ts
/**
 * 扫描整个文件，产出每列统计量。文件不上传——建模阶段数据不出用户的机器。
 *
 * 明确**不采样**：前 N 行不是随机样本。订单表通常按时间排序，前 1000 行
 * 可能只有 3 个州，基数估计会严重偏低，把本该是实体的列判成属性——而那
 * 不会报错，只会让本体建歪。
 *
 * xlsx 必须整个读进内存（二进制容器格式没法只读一段），所以对它加了体积
 * 上限；超过就抛错并说明原因，不能让页面静静地卡住。
 */
export async function scanTableFile(file: File): Promise<ColumnStats[]>
```

上限常量与实现细节：

```ts
/**
 * xlsx 的体积上限。它必须整个读进内存再解析，超过这个量级浏览器会卡死。
 * CSV 走流式读取，不受这个限制。
 */
export const MAX_XLSX_BYTES = 20 * 1024 * 1024
```

CSV 分块流式读取（用 `file.stream()` 或按 `slice` 递进），逐行喂给 `accumulateRow`；xlsx 用 SheetJS 读全表后逐行喂。两条路都走同一套累加器。

- [ ] **Step 8: 加读文件的测试**

```ts
describe('scanTableFile', () => {
  it('读 CSV，表头之外的每一行都算进去', async () => {
    const csv = 'state,revenue\nCA,10.5\nTX,20\nCA,30\n'
    const file = new File([csv], 'orders.csv', { type: 'text/csv' })
    const stats = await scanTableFile(file)
    const byName = Object.fromEntries(stats.map((s) => [s.name, s]))
    expect(byName.state.distinctCount).toBe(2)
    expect(byName.state.nonEmptyCount).toBe(3)
    expect(byName.revenue.inferredType).toBe('number')
  })

  it('带引号的字段按 CSV 规则解析', async () => {
    const csv = 'name,note\n"A,B","says ""hi"""\n'
    const file = new File([csv], 'x.csv', { type: 'text/csv' })
    const byName = Object.fromEntries((await scanTableFile(file)).map((s) => [s.name, s]))
    expect(byName.name.samples).toEqual(['A,B'])
  })

  it('超大 xlsx 明确拒绝，不是静静卡住', async () => {
    // xlsx 必须整个读进内存。页面卡死时用户不知道发生了什么，也不知道
    // 该怎么办；一条明确的错误信息至少告诉他换个小一点的文件。
    const big = new File([new Uint8Array(MAX_XLSX_BYTES + 1)], 'big.xlsx')
    await expect(scanTableFile(big)).rejects.toThrow(/过大|太大/)
  })
})
```

- [ ] **Step 9: 跑测试并提交**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/columnStats.test.ts && npx tsc --noEmit`

```bash
git add frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 列统计扫描

全量扫描，明确不采样：前 N 行不是随机样本，订单表按时间排序的话前 1000 行
可能只有 3 个州，基数估计严重偏低，把本该是实体的列判成属性——而那不会
报错，只会让本体建歪。

distinct 集合带上限（1000）：判定只需要知道低基数还是高基数，不封顶的话
一张百万行的表会把每列的所有值都留在内存里。

高基数的整数列一律当字符串：订单号、SKU 这类几乎总是标识而不是度量，判成
number 会让它被归进属性，那个实体类型就整个没了。

xlsx 有体积上限并明确报错——它必须整个读进内存，超了页面会卡死，而卡死时
用户不知道发生了什么。"
```

---

### Task 3: 列角色判定

**Files:**
- Create: `frontend/src/admin/guidedOntology/columnRoles.ts`
- Test: `frontend/src/admin/guidedOntology/columnRoles.test.ts`
- Modify: `frontend/src/admin/guidedOntology/types.ts`（加 `ColumnRole`）

**Interfaces:**
- Consumes: `ColumnStats`（Task 2）
- Produces:
  - `type ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'`
  - `interface RoledColumn { stats: ColumnStats; role: ColumnRole; reason: string }`
  - `assignRoles(stats: ColumnStats[], totalRows: number): RoledColumn[]`
  - `DIMENSION_MAX_RATIO = 0.2`

- [ ] **Step 1: 写失败的测试**

```ts
import { describe, expect, it } from 'vitest'
import { assignRoles } from './columnRoles'
import type { ColumnStats } from './types'

function stats(over: Partial<ColumnStats>): ColumnStats {
  return {
    name: 'c',
    nonEmptyCount: 1000,
    distinctCount: 50,
    distinctCapped: false,
    samples: [],
    inferredType: 'string',
    ...over,
  }
}

const roleOf = (s: ColumnStats, totalRows = 1000) => assignRoles([s], totalRows)[0].role

describe('自动判定，不需要问用户', () => {
  it('数值列是度量', () => {
    expect(roleOf(stats({ name: 'revenue', inferredType: 'number' }))).toBe('measure')
  })

  it('几乎每行一个值的字符串列是标识', () => {
    expect(roleOf(stats({ name: '订单号', distinctCount: 998, nonEmptyCount: 1000 }))).toBe(
      'identifier',
    )
  })

  it('封顶的字符串列也是标识——封顶意味着至少 1000 个不同值', () => {
    expect(
      roleOf(stats({ name: 'id', distinctCount: 1000, distinctCapped: true, nonEmptyCount: 1000 })),
    ).toBe('identifier')
  })

  it('日期列单独成一类', () => {
    // 不能混进 measure：数据模型没有日期类型，这一类要单独提示限制。
    expect(roleOf(stats({ name: 'purchase_date', inferredType: 'date' }))).toBe('date')
  })

  it('低基数字符串列是维度候选', () => {
    expect(roleOf(stats({ name: 'customer_state', distinctCount: 50 }))).toBe('dimension')
  })

  it('中等基数的字符串列是自由文本，不是维度', () => {
    // 一列有 600 个不同值（占 60%），建成实体类型会造出 600 个节点，
    // 而它们之间没有任何共性——那是备注，不是维度。
    expect(roleOf(stats({ name: 'note', distinctCount: 600, nonEmptyCount: 1000 }))).toBe('freetext')
  })
})

describe('每条判定都要带依据', () => {
  it('依据里有具体数字，不是一句空话', () => {
    // 用户要能据此推翻判定。"这是维度"没法推翻，"1000 行里 50 个不同值"
    // 可以——他知道自己的业务里州就是 50 个。
    const [roled] = assignRoles([stats({ name: 'customer_state', distinctCount: 50 })], 1000)
    expect(roled.reason).toMatch(/50/)
  })
})

describe('空列', () => {
  it('整列为空不判成任何有意义的角色', () => {
    // 判成维度的话会建出一个没有任何实例的实体类型。
    const role = roleOf(stats({ name: 'unused', nonEmptyCount: 0, distinctCount: 0 }))
    expect(role).toBe('freetext')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/columnRoles.test.ts`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

```ts
import type { ColumnRole, ColumnStats, RoledColumn } from './types'

/**
 * 不同值占非空行数的比例低于这个值，才算维度候选。
 *
 * 高于它的字符串列是自由文本（备注、描述）：把 600 个不同值的列建成实体
 * 类型，会造出 600 个彼此无关的节点。
 */
export const DIMENSION_MAX_RATIO = 0.2

/** 高于这个比例就认为"几乎每行一个"，是本行的标识。 */
const IDENTIFIER_MIN_RATIO = 0.9

export function assignRoles(stats: ColumnStats[], totalRows: number): RoledColumn[] {
  return stats.map((column) => {
    const { role, reason } = classify(column, totalRows)
    return { stats: column, role, reason }
  })
}

function classify(column: ColumnStats, totalRows: number): { role: ColumnRole; reason: string } {
  if (column.nonEmptyCount === 0) {
    // 建成实体类型的话，会造出一个没有任何实例的类型。
    return { role: 'freetext', reason: '这一列全是空的' }
  }
  if (column.inferredType === 'date') {
    return { role: 'date', reason: `识别为日期，样例 ${column.samples[0] ?? ''}` }
  }
  if (column.inferredType === 'number' || column.inferredType === 'integer') {
    return { role: 'measure', reason: '数值列，通常是度量' }
  }

  const ratio = column.distinctCount / column.nonEmptyCount
  if (column.distinctCapped || ratio >= IDENTIFIER_MIN_RATIO) {
    const count = column.distinctCapped ? `超过 ${column.distinctCount}` : `${column.distinctCount}`
    return {
      role: 'identifier',
      reason: `${column.nonEmptyCount} 个非空值里有 ${count} 个不同值，几乎每行一个`,
    }
  }
  if (ratio <= DIMENSION_MAX_RATIO) {
    return {
      role: 'dimension',
      reason: `${column.nonEmptyCount} 个非空值里只有 ${column.distinctCount} 个不同值，重复度高`,
    }
  }
  return {
    role: 'freetext',
    reason: `${column.nonEmptyCount} 个非空值里有 ${column.distinctCount} 个不同值，重复度不足以当分类`,
  }
}
```

`types.ts` 追加：

```ts
export type ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'

export interface RoledColumn {
  stats: ColumnStats
  role: ColumnRole
  /** 判定依据。必须带具体数字——用户要能据此推翻它。 */
  reason: string
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/columnRoles.test.ts`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认三条断言各自会红**

1. 把 `if (column.nonEmptyCount === 0)` 整段删掉 → 「整列为空」应 FAIL
2. 把 `ratio <= DIMENSION_MAX_RATIO` 改成 `ratio <= 0.9` → 「中等基数是自由文本」应 FAIL
3. 把 `reason` 里的数字去掉、改成固定文案 → 「依据里有具体数字」应 FAIL

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 列角色判定

数据形状能定的直接定（度量、标识、日期、自由文本），只有低基数字符串列
留给用户判断——那是唯一的模糊地带。

每条判定都带具体数字的依据。"这是维度"用户没法推翻，"1000 行里 50 个
不同值"可以：他知道自己业务里州就是 50 个。

日期单独成一类而不是混进度量：数据模型没有日期类型，这一类要单独提示
限制。"
```

---

### Task 4: 生成本体草案与 ETL 映射

**Files:**
- Create: `frontend/src/admin/guidedOntology/draftProposal.ts`
- Test: `frontend/src/admin/guidedOntology/draftProposal.test.ts`
- Modify: `frontend/src/admin/guidedOntology/types.ts`

**Interfaces:**
- Consumes: `RoledColumn`（Task 3）、`BuilderEntity` / `BuilderRelation`（`../schemaEtlConfigBuilder/types`）
- Produces:
  - `interface GuidedDecision { dimensionsAsEntity: Record<string, boolean>; parentOf: Record<string, string>; relationNameOf: Record<string, string> }`
  - `interface Proposal { termTypes: DraftTermType[]; relationTypes: DraftRelationType[]; constraints: DraftConstraint[]; unusedColumns: string[] }`
  - `buildProposal(roled: RoledColumn[], decision: GuidedDecision): Proposal`
  - `initialDecision(roled: RoledColumn[]): GuidedDecision`
  - `toEtlBuilder(roled, decision, fileId): { entities: BuilderEntity[]; relations: BuilderRelation[] }`
  - `suggestRelationName(subject: string, object: string): string`

- [ ] **Step 1: 写失败的测试**

用 demo 那张真实的表当样本：

```ts
import { describe, expect, it } from 'vitest'
import { buildProposal, initialDecision, suggestRelationName, toEtlBuilder } from './draftProposal'
import type { RoledColumn } from './types'

/** demo 租户那张电商订单宽表，本项目里真实存在的形状。 */
function demoColumns(): RoledColumn[] {
  const col = (name: string, role: RoledColumn['role'], distinctCount: number): RoledColumn => ({
    stats: {
      name,
      nonEmptyCount: 10000,
      distinctCount,
      distinctCapped: false,
      samples: [],
      inferredType: role === 'measure' ? 'number' : role === 'date' ? 'date' : 'string',
    },
    role,
    reason: '',
  })
  return [
    col('订单号', 'identifier', 9998),
    col('产品', 'dimension', 10),
    col('公司', 'dimension', 3),
    col('类目', 'dimension', 4),
    col('用户名', 'dimension', 800),
    col('revenue', 'measure', 500),
    col('units_sold', 'measure', 20),
    col('purchase_date', 'date', 300),
    col('customer_state', 'dimension', 50),
    col('internal_note', 'freetext', 6000),
  ]
}

describe('默认决定', () => {
  it('所有维度列默认建成实体类型', () => {
    // 少建是静默错误（那类问题就是答不出来，不报错），多建看得见
    // （实体列表里就有）。所以默认往实体偏。
    const decision = initialDecision(demoColumns())
    for (const name of ['产品', '公司', '类目', '用户名', 'customer_state']) {
      expect(decision.dimensionsAsEntity[name]).toBe(true)
    }
  })

  it('默认是星型：所有实体都挂在标识列下', () => {
    // 星型一定连通，不会漏掉任何实体；多一条冗余边是看得见的，用户一眼
    // 就能说"这条不对"。反过来默认不连、让用户自己加，漏掉的那条不会
    // 有任何提示。
    const decision = initialDecision(demoColumns())
    expect(decision.parentOf['产品']).toBe('订单号')
    expect(decision.parentOf['类目']).toBe('订单号')
  })
})

describe('生成草案', () => {
  it('标识列和被选中的维度都成为实体类型', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const values = proposal.termTypes.map((t) => t.value).sort()
    expect(values).toEqual(
      ['产品', '公司', '类目', '用户名', '订单号', 'customer_state'].sort(),
    )
  })

  it('度量和日期成为标识列的属性', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    const fieldNames = order.extra_fields.map((f) => f.name)
    expect(fieldNames).toContain('revenue')
    expect(fieldNames).toContain('purchase_date')
  })

  it('日期属性存成 string——数据模型没有日期类型', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    const date = order.extra_fields.find((f) => f.name === 'purchase_date')!
    expect(date.value_type).toBe('string')
  })

  it('维度改成属性之后，就不再是实体类型了', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['customer_state'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.termTypes.map((t) => t.value)).not.toContain('customer_state')
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    expect(order.extra_fields.map((f) => f.name)).toContain('customer_state')
  })

  it('改挂之后约束跟着变', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.parentOf['类目'] = '产品'
    decision.relationNameOf['类目'] = 'BELONG_TO'

    const proposal = buildProposal(roled, decision)

    expect(proposal.constraints).toContainEqual({
      subject_term_type: '产品',
      relation_type: 'BELONG_TO',
      object_term_type: '类目',
    })
    expect(proposal.constraints).not.toContainEqual(
      expect.objectContaining({ subject_term_type: '订单号', object_term_type: '类目' }),
    )
  })

  it('自由文本列不进本体，进未使用清单', () => {
    // 不显示的话，用户永远不知道自己丢了什么——他会在三个月后问
    // "为什么查不到内部备注"，而那一列从一开始就没被采纳。
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    expect(proposal.unusedColumns).toContain('internal_note')
  })

  it('每个关系类型只出现一次，哪怕用在多条边上', () => {
    // SOLD_BY 在 demo 里用了两次（订单→公司、产品→公司）。重复声明会撞
    // 主键 (tenant_id, relation_type, status)。
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.relationNameOf['公司'] = 'SOLD_BY'
    decision.parentOf['类目'] = '产品'
    decision.relationNameOf['类目'] = 'SOLD_BY'

    const proposal = buildProposal(roled, decision)

    const names = proposal.relationTypes.map((r) => r.relation_type)
    expect(new Set(names).size).toBe(names.length)
  })

  it('关系类型一律 allow_chain_query', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    for (const relation of proposal.relationTypes) {
      expect(relation.allow_chain_query).toBe(true)
    }
  })
})

describe('关系命名建议', () => {
  it('全大写、下划线，符合后端的校验', () => {
    // ^[A-Z][A-Z0-9_]{0,63}$ —— 不合规的名字后端会 400，而用户看不出
    // 是哪个字符的问题。
    const name = suggestRelationName('订单号', '产品')
    expect(name).toMatch(/^[A-Z][A-Z0-9_]{0,63}$/)
  })

  it('中文列名也能产出合规的名字', () => {
    expect(suggestRelationName('订单号', '用户名')).toMatch(/^[A-Z][A-Z0-9_]{0,63}$/)
  })
})

describe('顺带产出 ETL 映射', () => {
  it('每个实体类型都有对应的映射，属性列一并带上', () => {
    // 引导收集的信息已经够生成映射了。让用户在 ETL 页把同样的判断再做
    // 一遍是重复劳动，而且两次结果可能不一致。
    const roled = demoColumns()
    const { entities } = toEtlBuilder(roled, initialDecision(roled), 'file-1')
    const order = entities.find((e) => e.termType === '订单号')!
    expect(order.standardNameColumn).toBe('订单号')
    expect(order.nodeKeyParts).toEqual([{ kind: 'column', column: '订单号' }])
    expect(order.fieldMappings.revenue).toBe('revenue')
  })

  it('每条边都有对应的关系映射', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    const { relations } = toEtlBuilder(roled, decision, 'file-1')
    expect(relations).toContainEqual(
      expect.objectContaining({ subjectTermType: '订单号', objectTermType: '产品' }),
    )
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/draftProposal.test.ts`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现**

`types.ts` 追加：

```ts
export interface DraftExtraField {
  name: string
  value_type: 'string' | 'number' | 'integer' | 'number[]'
}

export interface DraftTermType {
  value: string
  extra_fields: DraftExtraField[]
  standard_name_value_type: 'string'
}

export interface DraftRelationType {
  relation_type: string
  example_phrase: string
  description: string
  allow_chain_query: true
}

export interface DraftConstraint {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface GuidedDecision {
  /** 每个维度列：建成实体类型（true）还是做成属性（false）。 */
  dimensionsAsEntity: Record<string, boolean>
  /** 每个非根实体挂在谁下面。键是实体名，值是父实体名。 */
  parentOf: Record<string, string>
  /** 每条边的关系类型名。键是子实体名。 */
  relationNameOf: Record<string, string>
}

export interface Proposal {
  termTypes: DraftTermType[]
  relationTypes: DraftRelationType[]
  constraints: DraftConstraint[]
  /** 没进本体的列。不显示等于静默丢弃。 */
  unusedColumns: string[]
  /** 属性名被清洗过的列：原列名 -> 清洗后的字段名。ETL 映射要用它对回去。 */
  renamedFields: Record<string, string>
  /** 没有标识列，根是猜的。UI 要提示用户确认。 */
  rootIsGuessed: boolean
}
```

`draftProposal.ts` 完整实现：

```ts
import type { BuilderEntity, BuilderRelation } from '../schemaEtlConfigBuilder/types'
import type {
  DraftConstraint,
  DraftExtraField,
  DraftRelationType,
  DraftTermType,
  GuidedDecision,
  Proposal,
  RoledColumn,
} from './types'

/**
 * 关系名建议。必须匹配后端的 ^[A-Z][A-Z0-9_]{0,63}$
 * （app/graphrag/ontology_relations.py:24）——不合规的名字后端会 400，
 * 而用户看不出是哪个字符出的问题。
 *
 * 中文列名产不出有意义的英文名，退回 RELATES_TO；用户可以在界面上改。
 */
export function suggestRelationName(subject: string, object: string): string {
  void subject
  const ascii = object
    .replace(/[^a-zA-Z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toUpperCase()
  if (ascii && /^[A-Z]/.test(ascii)) return `HAS_${ascii}`.slice(0, 64)
  return 'RELATES_TO'
}

/**
 * 把列名清洗成合法的属性字段名。
 *
 * 必须匹配 ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
 * （app/graphrag/ontology_categories.py:26）。中文列名会被清成空串，那时
 * 退回一个带序号的占位名——丢掉这一列更糟，用户至少能在界面上看到它被
 * 改成了什么。
 */
export function sanitizeFieldName(column: string, index: number): string {
  const cleaned = column.replace(/[^a-zA-Z0-9_]/g, '_').replace(/^[^a-zA-Z_]+/, '')
  if (cleaned === '') return `field_${index + 1}`
  return cleaned.slice(0, 64)
}

function rootOf(roled: RoledColumn[]): { name: string; guessed: boolean } {
  const identifier = roled.find((c) => c.role === 'identifier')
  if (identifier) return { name: identifier.stats.name, guessed: false }
  // 纯维度表（产品主数据这类）没有标识列。拿第一个维度当根，并标记为
  // 猜的——UI 要提示用户确认，否则结构会莫名其妙。
  const firstDimension = roled.find((c) => c.role === 'dimension')
  return { name: firstDimension?.stats.name ?? '', guessed: true }
}

export function initialDecision(roled: RoledColumn[]): GuidedDecision {
  const { name: root } = rootOf(roled)
  const dimensionsAsEntity: Record<string, boolean> = {}
  const parentOf: Record<string, string> = {}
  const relationNameOf: Record<string, string> = {}

  for (const column of roled) {
    if (column.role !== 'dimension') continue
    const name = column.stats.name
    // 默认建成实体：少建是静默错误（那类问题就是答不出来，不报错），
    // 多建看得见（实体列表里就有）。
    dimensionsAsEntity[name] = true
    if (name === root) continue
    // 默认星型：一定连通，不会漏掉任何实体；多一条冗余边是看得见的。
    parentOf[name] = root
    relationNameOf[name] = suggestRelationName(root, name)
  }
  return { dimensionsAsEntity, parentOf, relationNameOf }
}

function measureValueType(column: RoledColumn): DraftExtraField['value_type'] {
  // 日期存成 string：数据模型只有 string/number/integer/number[]，没有
  // 日期类型。这不是疏忽，是必须向用户明说的限制。
  if (column.role === 'date') return 'string'
  if (column.role === 'dimension') return 'string'
  return column.stats.inferredType === 'integer' ? 'integer' : 'number'
}

export function buildProposal(roled: RoledColumn[], decision: GuidedDecision): Proposal {
  const { name: root, guessed: rootIsGuessed } = rootOf(roled)

  const entityNames = new Set<string>()
  for (const column of roled) {
    if (column.role === 'identifier') entityNames.add(column.stats.name)
    if (column.role === 'dimension' && decision.dimensionsAsEntity[column.stats.name]) {
      entityNames.add(column.stats.name)
    }
  }

  // 属性一律挂在根实体上。度量、日期、以及被用户取消选中的维度列，描述的
  // 都是"这一行"，而这一行的身份就是根。
  const renamedFields: Record<string, string> = {}
  const rootFields: DraftExtraField[] = []
  const unusedColumns: string[] = []

  roled.forEach((column, index) => {
    const name = column.stats.name
    if (entityNames.has(name)) return
    const isAttribute =
      column.role === 'measure' ||
      column.role === 'date' ||
      (column.role === 'dimension' && !decision.dimensionsAsEntity[name])
    if (!isAttribute) {
      // 自由文本、空列：不进本体。必须列出来——不显示等于静默丢弃。
      unusedColumns.push(name)
      return
    }
    const fieldName = sanitizeFieldName(name, index)
    if (fieldName !== name) renamedFields[name] = fieldName
    rootFields.push({ name: fieldName, value_type: measureValueType(column) })
  })

  const termTypes: DraftTermType[] = [...entityNames].map((value) => ({
    value,
    // 属性只挂在根上。别的实体是维度，它们自己的属性得从别的表来。
    extra_fields: value === root ? rootFields : [],
    standard_name_value_type: 'string',
  }))

  const constraints: DraftConstraint[] = []
  const relationTypeByName = new Map<string, DraftRelationType>()

  for (const child of entityNames) {
    const parent = decision.parentOf[child]
    // parent === child 会造出自环 A-[R]->A，图谱查询会陷进去。
    if (!parent || parent === child || !entityNames.has(parent)) continue
    const relationType = decision.relationNameOf[child] ?? suggestRelationName(parent, child)
    constraints.push({
      subject_term_type: parent,
      relation_type: relationType,
      object_term_type: child,
    })
    // 去重：SOLD_BY 在 demo 里用了两次（订单->公司、产品->公司），重复
    // 声明会撞主键 (tenant_id, relation_type, status)。
    if (!relationTypeByName.has(relationType)) {
      relationTypeByName.set(relationType, {
        relation_type: relationType,
        example_phrase: `${parent} ${relationType} ${child}`,
        description: '',
        // 不暴露给用户：它是查询层的开关，普通用户没有判断依据。
        allow_chain_query: true,
      })
    }
  }

  return {
    termTypes,
    relationTypes: [...relationTypeByName.values()],
    constraints,
    unusedColumns,
    renamedFields,
    rootIsGuessed,
  }
}

/**
 * 顺带产出 ETL 映射。
 *
 * 引导收集的信息已经够生成映射了——让用户在 ETL 页把同样的判断（哪列是
 * 标识、哪列是属性）再做一遍是重复劳动，而且两次结果可能不一致，那时以
 * 哪个为准？
 */
export function toEtlBuilder(
  roled: RoledColumn[],
  decision: GuidedDecision,
  fileId: string,
): { entities: BuilderEntity[]; relations: BuilderRelation[] } {
  const proposal = buildProposal(roled, decision)
  const columnOfField = new Map(
    Object.entries(proposal.renamedFields).map(([column, field]) => [field, column]),
  )

  const entities: BuilderEntity[] = proposal.termTypes.map((termType) => ({
    id: `guided-${termType.value}`,
    termType: termType.value,
    fileId,
    // 实体名就是那一列本身。node_key 用同一列——引导只处理单表单列的简单
    // 情况，复合键要用户去 ETL 页自己配。
    standardNameColumn: termType.value,
    nodeKeyParts: [{ kind: 'column', column: termType.value }],
    fieldMappings: Object.fromEntries(
      termType.extra_fields.map((field) => [
        field.name,
        columnOfField.get(field.name) ?? field.name,
      ]),
    ),
  }))

  const relations: BuilderRelation[] = proposal.constraints.map((constraint, index) => ({
    id: `guided-rel-${index}`,
    fileId,
    subjectTermType: constraint.subject_term_type,
    relationType: constraint.relation_type,
    objectTermType: constraint.object_term_type,
  }))

  return { entities, relations }
}
```

**实现前必须先读 `frontend/src/admin/schemaEtlConfigBuilder/types.ts`**，确认 `BuilderEntity` / `BuilderRelation` 的字段名与上面一致；对不上以源码为准。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/draftProposal.test.ts`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认四条断言各自会红**

1. 把 `initialDecision` 里的默认值改成 `false` → 「默认建成实体类型」应 FAIL
2. 把日期的 `value_type` 改成 `'date'` → 「日期属性存成 string」应 FAIL
3. 把关系类型去重去掉 → 「每个关系类型只出现一次」应 FAIL
4. 把 `unusedColumns` 恒返回 `[]` → 「自由文本列进未使用清单」应 FAIL

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 从列角色生成本体草案与 ETL 映射

默认星型 + 默认建成实体：星型一定连通不会漏掉实体，多一条冗余边是看得见
的；少建实体则是静默错误，那类问题就是答不出来。

日期属性存成 string 并不是疏忽——数据模型只有 string/number/integer/
number[]，没有日期类型，「上个月的订单」在图谱层做不了范围过滤。UI 要
明说这个限制。

关系类型去重：SOLD_BY 在 demo 里用了两次（订单→公司、产品→公司），重复
声明会撞主键。

顺带产出 ETL 映射：引导收集的信息已经够了，让用户在 ETL 页把同样的判断
再做一遍是重复劳动，而且两次结果可能不一致。"
```

---

### Task 5: 引导页外壳与第一步（传表、扫描）

**Files:**
- Create: `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx`
- Modify: `frontend/src/adminRoutes.ts`（加 `guidedOntology: '/admin/model/guided'`）
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/adminRoutes.test.ts`（例外清单）
- Test: `frontend/src/admin/guidedOntology/guidedPage.test.tsx`

**Interfaces:**
- Consumes: `scanTableFile`（Task 2）、`assignRoles`（Task 3）、`initialDecision` / `buildProposal`（Task 4）
- Produces: `ADMIN_ROUTES.guidedOntology`、`GuidedOntologyPage`

- [ ] **Step 1: 写失败的测试**

```tsx
describe('引导页第一步', () => {
  it('一开始只要求传一张表', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.guidedOntology)
    expect(await screen.findByLabelText(/选择一张表/)).toBeTruthy()
  })

  it('扫描中显示进度，不是空白', async () => {
    // 扫描一张大表要几秒。什么都不显示的话用户会以为页面卡了，然后重复
    // 点击或刷新。
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), csvFile())
    expect(await screen.findByText(/正在扫描/)).toBeTruthy()
  })

  it('扫描失败时说清原因，不是静静停住', async () => {
    signIn('admin')
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.guidedOntology)
    await user.upload(await screen.findByLabelText(/选择一张表/), oversizedXlsx())
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('member 看到的是无权限提示，不是 404', async () => {
    signIn('member')
    renderAt(ADMIN_ROUTES.guidedOntology)
    expect(await screen.findByTestId('no-permission')).toBeTruthy()
    expect(screen.queryByTestId('not-found')).toBeNull()
  })
})
```

`csvFile()` / `oversizedXlsx()` 在测试文件里构造：

```ts
const csvFile = () =>
  new File(
    ['订单号,产品,revenue\n1001,咖啡,10.5\n1002,茶,20\n1003,咖啡,30\n'],
    'orders.csv',
    { type: 'text/csv' },
  )

const oversizedXlsx = () =>
  new File([new Uint8Array(21 * 1024 * 1024)], 'big.xlsx')
```

`signIn` / `renderAt` 沿用 `frontend/src/admin/accountsPage.test.tsx` 里的形态（`admin_session_token` / `admin_role` / `admin_current_tenant` 三个 sessionStorage 键 + 五层 Provider 包裹）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedPage.test.tsx`
Expected: FAIL

- [ ] **Step 3: 加路由常量与页面骨架**

`adminRoutes.ts` 的 `ADMIN_ROUTES` 里加 `guidedOntology: '/admin/model/guided'`。

**它属于「建模」组，但不进 `NAV_GROUPS`**——它是入口，不是常驻目的地。在 `adminRoutes.test.ts` 的 `NOT_IN_NAV` 里加一行并写明理由：

```ts
      guidedOntology: '首次建模的入口，从本体结构页进入；不是常驻目的地',
```

`EXTRA_TITLES` 加 `guidedOntology: '引导建模'`。

`App.tsx` 在 `model/graph` 那条之后加 `<Route path="model/guided" element={<GuidedOntologyPage />} />`。

- [ ] **Step 4: 实现第一步**

页面用一个 `step` 状态机（`'upload' | 'scanning' | 'review' | 'submitting'`）。第一步只做：文件输入 → `scanTableFile` → `assignRoles` → `initialDecision` → 进入 review。

权限判断与账号页一致：`role !== 'admin'` 直接返回 `<div data-testid="no-permission">`。扫描失败渲染 `role="alert"` 并给出 `error.message`。

- [ ] **Step 5: 跑测试确认通过并破坏验证**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedPage.test.tsx`

破坏验证：把扫描失败的 `catch` 分支去掉（让异常冒泡）→「扫描失败时说清原因」应 FAIL。确认后恢复。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/guidedOntology/ frontend/src/adminRoutes.ts frontend/src/adminRoutes.test.ts frontend/src/App.tsx
git commit -m "feat(frontend): 引导页外壳与传表扫描

扫描一张大表要几秒，必须显示进度——什么都不显示的话用户会以为页面卡了，
然后重复点击或刷新。失败也要说清原因，不能静静停住。"
```

---

### Task 6: 审阅视图

**Files:**
- Create: `frontend/src/admin/guidedOntology/ProposalReview.tsx`
- Test: `frontend/src/admin/guidedOntology/proposalReview.test.tsx`

**Interfaces:**
- Consumes: `RoledColumn`、`GuidedDecision`、`Proposal`（Task 3、4）
- Produces: `<ProposalReview roled decision onDecisionChange proposal />`

- [ ] **Step 1: 写失败的测试**

```tsx
describe('低基数列的选择', () => {
  it('把两条路的能力差别摆出来，而不是问"实体还是属性"', async () => {
    // 问"该是实体还是属性"用户答不了——那是建模术语。问"你会不会问
    // 「加州有哪些客户」"他答得了。
    renderReview()
    const block = await screen.findByTestId('dimension-customer_state')
    expect(block.textContent).toMatch(/加州|哪些|能问/)
  })

  it('默认选中「建成实体」', async () => {
    renderReview()
    const radio = await screen.findByRole('radio', { name: /建成实体/ })
    expect((radio as HTMLInputElement).checked).toBe(true)
  })

  it('显示判定依据里的具体数字', async () => {
    renderReview()
    expect((await screen.findByTestId('dimension-customer_state')).textContent).toMatch(/50/)
  })
})

describe('层级', () => {
  it('每个实体能选挂在谁下面', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    renderReview({ onDecisionChange: onChange })
    await user.selectOptions(await screen.findByLabelText('类目 挂在'), '产品')
    expect(onChange).toHaveBeenCalled()
  })

  it('不能把实体挂在自己下面', async () => {
    // 自环会让约束表里出现 A-[R]->A，图谱查询会陷进去。
    renderReview()
    const select = (await screen.findByLabelText('类目 挂在')) as HTMLSelectElement
    const values = [...select.options].map((o) => o.value)
    expect(values).not.toContain('类目')
  })

  it('标识列是根，没有「挂在」选择', async () => {
    renderReview()
    expect(screen.queryByLabelText('订单号 挂在')).toBeNull()
  })

  it('已经用过的关系名要能选，不用重打', async () => {
    // SOLD_BY 在 demo 里用了两次（订单->公司、产品->公司）。不给选的话
    // 用户第二次会打出 SELL_BY，建出两个同义关系——图谱里同一件事有两种
    // 边，查询时漏掉一半而不报错。
    renderReview({
      decision: { ...baseDecision, relationNameOf: { ...baseDecision.relationNameOf, 公司: 'SOLD_BY' } },
    })
    const input = await screen.findByLabelText('类目 的关系名')
    const listId = input.getAttribute('list')
    expect(listId).toBeTruthy()
    const options = [...document.querySelectorAll(`#${listId} option`)].map((o) =>
      o.getAttribute('value'),
    )
    expect(options).toContain('SOLD_BY')
  })
})

describe('未使用的列', () => {
  it('列出来，不静静丢弃', async () => {
    // 不显示的话，用户永远不知道自己丢了什么——他会在三个月后问
    // "为什么查不到内部备注"，而那一列从一开始就没被采纳。
    renderReview()
    const unused = await screen.findByTestId('unused-columns')
    expect(unused.textContent).toMatch(/internal_note/)
  })

  it('未使用列为空时也要说一句，不是留白', async () => {
    renderReview({ proposal: { ...baseProposal, unusedColumns: [] } })
    expect((await screen.findByTestId('unused-columns')).textContent).toMatch(/都用上了|没有/)
  })
})

describe('日期列的限制', () => {
  it('明说范围过滤做不了', async () => {
    // 数据模型没有日期类型。不说的话，用户会以为"上个月的订单"这类问题
    // 能答，直到真去问才发现不行。
    renderReview()
    expect((await screen.findByTestId('date-warning')).textContent).toMatch(/范围|区间|过滤/)
  })
})
```

`renderReview()` 用 Task 4 测试里那份 demo 列构造 props。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/proposalReview.test.tsx`
Expected: FAIL — 组件不存在

- [ ] **Step 3: 实现 ProposalReview.tsx**

四个区块，顺序即阅读顺序：维度选择 → 层级 → 日期警告 → 未使用列。

```tsx
import type { GuidedDecision, Proposal, RoledColumn } from './types'

const card = 'rounded-card border border-subtle bg-card p-4'
const sectionTitle = 'font-mono text-sm font-bold uppercase tracking-wide text-ink-soft'

interface Props {
  roled: RoledColumn[]
  decision: GuidedDecision
  onDecisionChange: (next: GuidedDecision) => void
  proposal: Proposal
}

export function ProposalReview({ roled, decision, onDecisionChange, proposal }: Props) {
  const dimensions = roled.filter((c) => c.role === 'dimension')
  const dateColumns = roled.filter((c) => c.role === 'date')
  const entityNames = proposal.termTypes.map((t) => t.value)
  const rootName = entityNames.find(
    (name) => !Object.prototype.hasOwnProperty.call(decision.parentOf, name),
  )

  const setDecision = (patch: Partial<GuidedDecision>) =>
    onDecisionChange({ ...decision, ...patch })

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-3">
        <h2 className={sectionTitle}>这几列，你想怎么用</h2>
        {dimensions.map((column) => {
          const name = column.stats.name
          const asEntity = decision.dimensionsAsEntity[name]
          return (
            <div key={name} data-testid={`dimension-${name}`} className={`${card} flex flex-col gap-2`}>
              <div className="flex flex-wrap items-baseline gap-2">
                <code className="font-mono font-bold text-ink">{name}</code>
                {/* 依据必须带具体数字——"这是维度"用户没法推翻，
                    "10000 行里 50 个不同值"可以：他知道自己业务里州就是
                    50 个。 */}
                <span className="text-xs text-ink-soft">{column.reason}</span>
                {column.stats.samples.length > 0 && (
                  <span className="text-xs text-ink-faint">
                    样例：{column.stats.samples.slice(0, 3).join('、')}
                  </span>
                )}
              </div>
              {/* 不问"该是实体还是属性"——那是建模术语，用户答不了。
                  问他会不会问某类问题，他答得了。 */}
              <label className="flex items-start gap-2 text-sm text-ink">
                <input
                  type="radio"
                  name={`dim-${name}`}
                  checked={asEntity}
                  onChange={() =>
                    setDecision({
                      dimensionsAsEntity: { ...decision.dimensionsAsEntity, [name]: true },
                    })
                  }
                />
                <span>
                  <strong>建成实体</strong>——能问「{column.stats.samples[0] ?? '某个值'}
                  下面有哪些」「哪个{name}最多」这类问题
                </span>
              </label>
              <label className="flex items-start gap-2 text-sm text-ink">
                <input
                  type="radio"
                  name={`dim-${name}`}
                  checked={!asEntity}
                  onChange={() =>
                    setDecision({
                      dimensionsAsEntity: { ...decision.dimensionsAsEntity, [name]: false },
                    })
                  }
                />
                <span>
                  <strong>做成属性</strong>——只能作为过滤条件，问不出上面那些
                </span>
              </label>
            </div>
          )
        })}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className={sectionTitle}>它们怎么连起来</h2>
        {proposal.rootIsGuessed && (
          <p role="alert" className={`${card} text-sm text-ink`}>
            这张表里没有一列是「每行一个值」的标识，所以「{rootName}」是猜的。
            如果它不该是中心，请回上一步换一张表。
          </p>
        )}
        {entityNames
          .filter((name) => name !== rootName)
          .map((name) => (
            <div key={name} className={`${card} flex flex-wrap items-center gap-2`}>
              <label htmlFor={`parent-${name}`} className="text-sm font-bold text-ink">
                {name} 挂在
              </label>
              <select
                id={`parent-${name}`}
                value={decision.parentOf[name] ?? rootName ?? ''}
                onChange={(event) =>
                  setDecision({ parentOf: { ...decision.parentOf, [name]: event.target.value } })
                }
                className="rounded-control border border-subtle bg-paper px-2 py-1 text-sm text-ink"
              >
                {/* 排掉自己：自环会让约束表里出现 A-[R]->A，图谱查询会
                    陷进去。 */}
                {entityNames
                  .filter((candidate) => candidate !== name)
                  .map((candidate) => (
                    <option key={candidate} value={candidate}>
                      {candidate}
                    </option>
                  ))}
              </select>
              <span className="text-sm text-ink-soft">下面，关系叫</span>
              {/* 带 datalist：已经用过的关系名要能选。SOLD_BY 在 demo 里
                  用了两次（订单->公司、产品->公司），不给选的话用户第二次
                  会打出 SELL_BY，建出两个同义关系——图谱里同一件事有两种
                  边，查询时漏掉一半而不报错。 */}
              <input
                aria-label={`${name} 的关系名`}
                list="guided-relation-names"
                value={decision.relationNameOf[name] ?? ''}
                onChange={(event) =>
                  setDecision({
                    relationNameOf: {
                      ...decision.relationNameOf,
                      [name]: event.target.value.toUpperCase(),
                    },
                  })
                }
                className="rounded-control border border-subtle bg-paper px-2 py-1 font-mono text-sm text-ink"
              />
            </div>
          ))}
        <datalist id="guided-relation-names">
          {[...new Set(Object.values(decision.relationNameOf))]
            .filter(Boolean)
            .map((relationName) => (
              <option key={relationName} value={relationName} />
            ))}
        </datalist>
      </section>

      {dateColumns.length > 0 && (
        <section data-testid="date-warning" className={`${card} flex flex-col gap-1`}>
          <h2 className={sectionTitle}>日期列的限制</h2>
          {/* 不说的话，用户会以为"上个月的订单"这类问题能答，直到真去问
              才发现不行。 */}
          <p className="text-sm text-ink">
            {dateColumns.map((c) => c.stats.name).join('、')} 会被存成文本。
            系统目前没有日期类型，所以**按时间范围过滤**（「上个月的」「今年以来的」）
            在图谱层做不了，只能精确匹配。
          </p>
        </section>
      )}

      <section data-testid="unused-columns" className={`${card} flex flex-col gap-1`}>
        <h2 className={sectionTitle}>没有用到的列</h2>
        {/* 不显示等于静默丢弃：用户会在三个月后问"为什么查不到内部备注"，
            而那一列从一开始就没被采纳。 */}
        {proposal.unusedColumns.length === 0 ? (
          <p className="text-sm text-ink-soft">这张表的列都用上了。</p>
        ) : (
          <>
            <p className="text-sm text-ink-soft">
              这些列没有进入本体——它们的重复度不足以当分类，也不是数值。
              如果其中有你需要的，回上一步换一张更聚焦的表，或者建完之后去
              「本体结构」页手工加。
            </p>
            <ul className="flex flex-wrap gap-2">
              {proposal.unusedColumns.map((name) => (
                <li
                  key={name}
                  className="rounded-chip border border-subtle bg-paper px-2 py-0.5 font-mono text-xs text-ink-soft"
                >
                  {name}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  )
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/proposalReview.test.tsx`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认三条断言各自会红**

1. 把整个 `data-testid="unused-columns"` 区块删掉 → 两条相关断言应 FAIL
2. 把 `.filter((candidate) => candidate !== name)` 去掉 → 「不能挂在自己下面」应 FAIL
3. 把 `data-testid="date-warning"` 区块删掉 → 「明说范围过滤做不了」应 FAIL
4. 把关系名输入上的 `list="guided-relation-names"` 去掉 → 「已经用过的关系名要能选」应 FAIL

每处确认后恢复。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 草案审阅视图

低基数列不问"该是实体还是属性"——那是建模术语，用户答不了。问他会不会
问「加州有哪些客户」这类问题，他答得了。每条判定都摆出具体数字，用户要
能据此推翻它。

未使用列必须列出来：不显示等于静默丢弃，用户会在三个月后问"为什么查不到
内部备注"，而那一列从一开始就没被采纳。

日期列的限制必须明说：数据模型没有日期类型，按时间范围过滤做不了。不说
的话用户会以为能答，直到真去问才发现。

"挂在"下拉排掉自己：自环会让约束表里出现 A-[R]->A，图谱查询会陷进去。"
```

---

### Task 7: 提交草稿并衔接 ETL

**Files:**
- Modify: `frontend/src/admin/guidedOntology/GuidedOntologyPage.tsx`
- Test: `frontend/src/admin/guidedOntology/guidedSubmit.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `POST /{tenant_id}/draft/replace`、Task 4 的 `buildProposal` / `toEtlBuilder`、现有的 `buildConfigYaml`
- Produces: 无（终点）

- [ ] **Step 1: 写失败的测试**

```tsx
describe('提交草稿', () => {
  it('一次请求写入整套本体', async () => {
    // 逐个写的话中途失败会留下半份草稿，而 checkout 不会清空它。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => expect(replaceCalls.length).toBe(1))
    const body = replaceCalls[0]
    expect(body.term_types.map((t) => t.value)).toContain('订单号')
    expect(body.constraints.length).toBeGreaterThan(0)
  })

  it('写入的是草稿，不是直接确认', async () => {
    // 确认是不可逆的（旧的已确认版本会被换掉）。引导不该替用户做这个
    // 决定。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    await waitFor(() => expect(replaceCalls.length).toBe(1))
    expect(confirmCalls.length).toBe(0)
  })

  it('写入失败时不跳走，错误留在页面上', async () => {
    // 跳走的话用户以为成功了，回头发现草稿是空的。
    stubReplace(400, { detail: '约束引用了未声明的实体类型：幽灵' })
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('button', { name: /写入草稿/ })).toBeTruthy()
  })

  it('成功后提示下一步是确认，并给出去处', async () => {
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('link', { name: /本体结构|去确认/ })).toBeTruthy()
  })

  it('成功后提供 ETL 映射下载，不用重配', async () => {
    // 引导收集的信息已经够生成映射了。让用户在 ETL 页把同样的判断再做
    // 一遍是重复劳动，而且两次结果可能不一致。
    const user = userEvent.setup()
    renderAtReviewStep()
    await user.click(await screen.findByRole('button', { name: /写入草稿/ }))
    expect(await screen.findByRole('button', { name: /映射配置|下载配置/ })).toBeTruthy()
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedSubmit.test.tsx`
Expected: FAIL

- [ ] **Step 3: 实现提交与成功态**

加到 `GuidedOntologyPage.tsx`：

```tsx
const handleSubmit = async () => {
  if (!sessionToken || submitting) return
  setSubmitting(true)
  setError(null)
  try {
    const proposal = buildProposal(roled, decision)
    const response = await adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/draft/replace`,
      sessionToken,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          term_types: proposal.termTypes,
          relation_types: proposal.relationTypes,
          constraints: proposal.constraints,
        }),
      },
    )
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(extractErrorDetail(body, '写入草稿失败'))
    }
    // 刻意**不**调 /confirm：确认是不可逆的（旧的已确认版本会被换掉），
    // 引导不该替用户做这个决定。
    setStep('done')
  } catch (err) {
    // 失败时留在原地：跳走的话用户以为成功了，回头发现草稿是空的。
    setError(err instanceof Error ? err.message : '写入草稿失败')
  } finally {
    setSubmitting(false)
  }
}

const handleDownloadMapping = () => {
  const { entities, relations } = toEtlBuilder(roled, decision, fileId)
  const yaml = buildConfigYaml({ tenantId, entities, relations, files })
  const blob = new Blob([yaml], { type: 'text/yaml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `${tenantId}-etl-config.yaml`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
```

成功态（`step === 'done'`）渲染两个去处：

```tsx
<div className="flex flex-col gap-3">
  <p className="text-sm text-ink">
    本体草稿已写入。它还没有生效——去「本体结构」页核对一遍再确认。
    确认是不可逆的：旧的已确认版本会被换掉。
  </p>
  <div className="flex flex-wrap gap-2">
    <Link to={ADMIN_ROUTES.ontology} className={buttonClass}>
      去本体结构页确认
    </Link>
    {/* 引导收集的信息已经够生成映射了，不用在 ETL 页重配一遍。 */}
    <button type="button" onClick={handleDownloadMapping} className={buttonClass}>
      下载 ETL 映射配置
    </button>
  </div>
</div>
```

`buildConfigYaml` 的参数形状以 `frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts` 的实际签名为准——**先读它**，`files` 那一项是用来把 `fileId` 映射回文件名的。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedSubmit.test.tsx`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认两条断言各自会红**

1. 在 `setStep('done')` 之前加一句 `await adminFetch(\`/api/admin/ontology/${tenantId}/confirm\`, sessionToken, { method: 'POST' })` → 「写入的是草稿，不是直接确认」应 FAIL
2. 把 `catch` 里的 `setError` 换成 `setStep('done')` → 「写入失败时不跳走」应 FAIL

每处确认后恢复。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 写入草稿并衔接 ETL

一次请求写入整套本体——逐个写的话中途失败会留下半份草稿，而 checkout
不会清空它。

刻意不调 /confirm：确认是不可逆的（旧的已确认版本会被换掉），引导不该替
用户做这个决定。写入失败时留在原地，跳走的话用户以为成功了，回头发现草稿
是空的。

成功后提供 ETL 映射下载：引导收集的信息已经够生成映射了，让用户在 ETL 页
把同样的判断再做一遍是重复劳动，而且两次结果可能不一致。"
```

---

### Task 8: 入口接线

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`
- Test: `frontend/src/admin/guidedOntology/guidedEntry.test.tsx`

- [ ] **Step 1: 写失败的测试**

```tsx
describe('引导入口', () => {
  it('本体结构页有引导入口', async () => {
    signIn('admin')
    renderAt(ADMIN_ROUTES.ontology)
    expect(await screen.findByRole('link', { name: /引导|从表格开始/ })).toBeTruthy()
  })

  it('已经有草稿时，入口要提示会被覆盖', async () => {
    // replace_draft 是整份替换。不提示的话，用户点进引导、走完流程，
    // 手工建的那些东西没了，而他不知道是这一步干的。
    signIn('admin')
    stubOntology({ termTypes: [{ value: '已有类型', extra_fields: [] }] })
    renderAt(ADMIN_ROUTES.ontology)
    const link = await screen.findByRole('link', { name: /引导|从表格开始/ })
    expect(link.getAttribute('title')).toMatch(/覆盖|替换/)
  })

  it('草稿为空时不提示覆盖——没有东西可覆盖', async () => {
    signIn('admin')
    stubOntology({ termTypes: [] })
    renderAt(ADMIN_ROUTES.ontology)
    const link = await screen.findByRole('link', { name: /引导|从表格开始/ })
    expect(link.getAttribute('title') ?? '').not.toMatch(/覆盖/)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedEntry.test.tsx`
Expected: FAIL

- [ ] **Step 3: 加入口**

`OntologySchemaPage.tsx` 的 `<h1>` 下面加一行。该页面已经拿到了三个 tab 的
数据，用它判断草稿是否为空——**先读该文件确认变量名**，下面的
`hasDraftContent` 是示意：

```tsx
{/* 引导负责从零到一；三个 tab 负责后续微调。两条路径都留着，因为它们
    的用户和场景确实不同。 */}
<Link
  to={ADMIN_ROUTES.guidedOntology}
  // replace_draft 是整份替换。不提示的话，用户走完引导，手工建的那些
  // 东西没了，而他不知道是这一步干的。
  title={
    hasDraftContent
      ? '从一张业务表开始重新推导本体——当前草稿会被整份覆盖'
      : '从一张业务表开始，平台会推荐一套本体草案'
  }
  className="flex items-center gap-1.5 self-start rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition hover:bg-interactive-hover"
>
  <Wand2 aria-hidden="true" className="h-4 w-4" />
  从表格开始引导建模
</Link>
```

`Wand2` 从 `lucide-react` 导入。`hasDraftContent` 由该页已有的实体类型列表
长度得出（`view === 'draft'` 时的那份数据）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npx vitest run src/admin/guidedOntology/guidedEntry.test.tsx`
Expected: 全部 passed

- [ ] **Step 5: 破坏实现，确认断言会红**

把 `title` 的三元判断改成恒返回带「覆盖」的那句 → 「草稿为空时不提示覆盖」
应 FAIL。确认后恢复。

- [ ] **Step 6: 前端全量与提交**

Run: `cd frontend && npm test && npm run typecheck && npm run build`

```bash
git add frontend/src/admin/OntologySchemaPage.tsx frontend/src/admin/guidedOntology/
git commit -m "feat(frontend): 本体结构页的引导入口

草稿非空时提示会被覆盖：replace_draft 是整份替换，不提示的话用户走完引导
回来发现手工建的东西没了，而他不知道是这一步干的。"
```

---

## 阶段验收

```bash
PYTHONIOENCODING=utf-8 timeout 400 .venv/Scripts/python.exe -m pytest -q > /tmp/full.log 2>&1
grep -E "passed|failed" /tmp/full.log | tail -1

cd frontend && npm test && npm run typecheck && npm run build
```

**手工验证**（自动化测不到真实文件与浏览器行为）：

1. 用真实的 `docs/demo-data/` 里的表（若有）或自己造一张 5000 行的 CSV，走完整条引导
2. 确认扫描不卡页面，进度可见
3. 确认低基数列的选择项文案说得通、依据数字对得上
4. 把「类目」改挂到「产品」下，确认最终写入的约束是 `产品 -[?]-> 类目` 而不是 `订单号 -[?]-> 类目`
5. 写入草稿后去本体结构页，确认三个 tab 里能看到引导建的内容
6. 确认「未使用的列」列出的确实是没被用上的那些
7. 下载 ETL 映射配置，去表格导入页传同一张表跑一次 dry run
8. 传一个 25MB 的 xlsx，确认拒绝并给出可读的原因，页面没卡死

**若第 5 步看不到内容**：查 `replace_draft` 有没有标记 checkout 状态——没标记的话，本体结构页那三个 tab 各自发的 `checkout` 会把已确认版本复制回来盖掉引导写的草稿。
