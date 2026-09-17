# 建模工作台数据面板补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 v1 建模工作台补上三条终审点名的能力——别名命不中时手动指列（含别名编辑与列角色依据显示）、投影关系映射前检查宾语键列是否在主语表、空白起步时用 `buildProposal` 推骨架。

**Architecture:** 全部在前端 `frontend/src/admin/modelingWorkbench/` 内完成：`sources[]` 条目多记一份 `columns`（角色/依据/类型），新增两个纯函数模块（`columnAssign.ts`、`blankStart.ts`），`projectToDraft.ts` 的关系投影加列存在性检查并返回被跳过的关系，三个面板各加一小块 UI。后端一行不改（`validate_state` 对多出来的键放行）。

**Tech Stack:** React 18 + TypeScript + vitest + @testing-library/react；复用 `guidedOntology/draftProposal.ts` 的 `buildProposal` / `initialDecision` / `sanitizeFieldName`。

**Spec:** `docs/superpowers/specs/2026-09-17-workbench-data-panel-completion-design.md`

## Global Constraints

- **后端不改**：`state_json` 只加键不改形状，`validate_state` 放行多出来的键（spec 决策 5）。唯一允许碰后端的是加一条"多出来的键能原样存取"的测试。
- **命中规则仍是确定性的**：手动指列是用户的决定，写进别名后下次照样走精确匹配；本计划不引入任何猜测。
- **纯函数原样返回同一引用表示"什么都没变"**（沿用 `skeletonEdits.ts` 的约定），调用方据此提示。
- **所有编辑立刻整份 PUT 回后端**（沿用页面的 `persist`），不做本地草稿。
- **约束的审阅随引用元素走**（终审裁定，已实现）：本计划新造的约束一律 `review: 'pending'`。
- 进程约束沿用主计划：`git add` 逐文件点名、不碰 `docs/superpowers/` 下用户未提交改动、不推 origin、不跑 `npx prettier --write`、前端测试 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run <path> --maxWorkers=2`、后端测试 `PYTHONIOENCODING=utf-8 python -u -m pytest <path> -q`、每个关键行为做变异测试、注释解释"为什么"且不写未经验证的因果、commit 结尾 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。

## File Structure

- Modify `types.ts` — `WorkspaceSource` 加 `columns?: SourceColumn[]`；新增 `SourceColumn`、`SkippedRelation`
- Create `columnAssign.ts` — `assignColumnAsKey` / `assignColumnAsField` / `setKeyAliases` / `setFieldAliases` / `parseAliasText`
- Create `blankStart.ts` — `proposalToWorkspace`
- Modify `projectToDraft.ts` — `projectToEtlYaml` 返回值加 `skippedRelations`
- Modify `panels/DataPanel.tsx` — 扫描时记 `columns`；未接住列显示角色/依据 + 「指给…」；空白起步分支
- Modify `panels/SkeletonPanel.tsx` — 别名编辑框
- Modify `panels/ApplyPanel.tsx` — 显示被跳过的关系
- Modify `ModelingWorkbenchPage.tsx` — 四个新回调；`handleApply` 把 `skippedRelations` 交给 ApplyPanel
- Modify `frontend/src/admin/guidedOntology/columnRoles.ts:25-27` — 过期 docstring
- Tests: `columnAssign.test.ts`、`blankStart.test.ts`、`projectToDraft.test.ts`（追加）、`workbenchPage.test.tsx`（追加）、`tests/graphrag/test_ontology_modeling_workspace.py`（追加一条）

---

### Task 1: `sources[].columns` 落盘

**Files:**
- Modify: `frontend/src/admin/modelingWorkbench/types.ts`
- Modify: `frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx`（`handleScan` 里构造 `withSource` 那段）
- Test: `frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx`（追加一条）
- Test: `tests/graphrag/test_ontology_modeling_workspace.py`（追加一条）

**Interfaces:**
- Produces:
  ```ts
  export interface SourceColumn { name: string; role: ColumnRole; reason: string; inferred_type: InferredType }
  export interface WorkspaceSource { file; sheet?; header_row?; first_data_row?; columns?: SourceColumn[] }
  export function columnsOf(roled: RoledColumn[]): SourceColumn[]
  ```

- [ ] **Step 1: 写失败的测试**

后端（追加到 `tests/graphrag/test_ontology_modeling_workspace.py` 末尾）：

```python
async def test_save_keeps_extra_keys_inside_sources_entries():
    """前端会往 sources[] 条目里多放 columns（列角色/依据），后端只校验外形，
    多出来的键必须原样存取——丢了的话工作台重新打开时未接住列旁的依据全没了。"""
    conn = await _conn()
    created = await create_workspace(conn, "t1", skill=None, actor="alice", now="2026-09-17T10:00:00")
    state = {
        "sources": [
            {
                "file": "a.csv",
                "header_row": 6,
                "columns": [{"name": "JAN", "role": "identifier", "reason": "100/100", "inferred_type": "string"}],
            }
        ]
    }
    saved = await save_workspace(
        conn, "t1", state=state, expected_updated_at=created.updated_at, actor="alice", now="2026-09-17T10:01:00"
    )
    assert saved.state["sources"][0]["columns"][0]["reason"] == "100/100"
    assert (await get_workspace(conn, "t1")).state["sources"][0]["columns"][0]["role"] == "identifier"
```

前端（追加到 `workbenchPage.test.tsx` 的 `describe('建模工作台')`）。先看文件里现有的 `csvFile` 之类夹具——如果没有，加一个 25 行的 CSV 夹具（照 `guidedOntology/columnRoles.test.ts` 里的写法：`订单号` 纯数字 1001…1025、`产品` 三个取值循环、`revenue` 带小数）：

```tsx
  it('扫描一张表后把每列的角色和依据存进 sources', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '数据' }))
    await userEvent.upload(screen.getByLabelText('选择数据表'), csvFile())
    await userEvent.click(await screen.findByRole('button', { name: '扫描并对齐' }))
    await waitFor(() => expect(saved.length).toBeGreaterThan(0))
    const body = saved[saved.length - 1] as {
      state: { sources: { file: string; columns?: { name: string; role: string; reason: string }[] }[] }
    }
    const source = body.state.sources.find((s) => s.file === 'orders.csv')!
    expect(source.columns?.map((c) => c.name)).toEqual(['订单号', '产品', 'revenue'])
    const product = source.columns!.find((c) => c.name === '产品')!
    expect(product.role).toBe('dimension')
    // 依据必须带具体数字——用户要能据此推翻判定
    expect(product.reason).toMatch(/\d/)
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `PYTHONIOENCODING=utf-8 python -u -m pytest tests/graphrag/test_ontology_modeling_workspace.py -q -k extra_keys`（应已通过——后端本来就放行；若通过，说明后端不需要改，这条测试是回归保护，继续）
Run（`frontend/`）：`NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/workbenchPage.test.tsx --maxWorkers=2 -t "存进 sources"`
Expected: FAIL，`source.columns` 为 `undefined`

- [ ] **Step 3: 改类型与 DataPanel**

`types.ts`：

```ts
import type { ColumnRole, InferredType, RoledColumn } from '../guidedOntology/types'

/** 扫描时算出的一列：角色、判定依据、推断类型。存下来是为了不重扫就能显示依据、
 *  投影关系时判断主语表有没有宾语键列、手动指字段时推 value_type。 */
export interface SourceColumn {
  name: string
  role: ColumnRole
  /** 判定依据，必须带具体数字——用户要能据此推翻它（主 spec 对 reason 的要求）。 */
  reason: string
  inferred_type: InferredType
}

export interface WorkspaceSource {
  file: string
  sheet?: string | number | null
  header_row?: number
  first_data_row?: number
  /** v1 存下的旧工作区没有这个键，视为"未知"。 */
  columns?: SourceColumn[]
}

export function columnsOf(roled: RoledColumn[]): SourceColumn[] {
  return roled.map((c) => ({
    name: c.stats.name,
    role: c.role,
    reason: c.reason,
    inferred_type: c.stats.inferredType,
  }))
}
```

`DataPanel.tsx` `handleScan` 里 `sources` 条目加 `columns: columnsOf(table.roled)`（import `columnsOf` from `'../types'`）。

- [ ] **Step 4: 跑测试确认通过**

Run 两条命令同上。Expected: PASS

- [ ] **Step 5: 变异检查**

把 `columns: columnsOf(table.roled)` 改成 `columns: []`：前端那条必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/types.ts frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx tests/graphrag/test_ontology_modeling_workspace.py
git commit -m "feat(admin): 建模工作台扫描时把列角色与判定依据存进工作区

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 手动指列与别名编辑的纯函数

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/columnAssign.ts`
- Test: `frontend/src/admin/modelingWorkbench/columnAssign.test.ts`

**Interfaces:**
- Consumes: `guidedOntology/draftProposal.ts` 的 `sanitizeFieldName(column, index)`；Task 1 的 `SourceColumn`
- Produces（全部纯函数，非法输入原样返回同一引用）:
  ```ts
  export function assignColumnAsKey(state, file, column, termValue): WorkspaceState
  export function assignColumnAsField(state, file, column, termValue): WorkspaceState
  export function setKeyAliases(state, termValue, aliases: string[]): WorkspaceState
  export function setFieldAliases(state, termValue, fieldName, aliases: string[]): WorkspaceState
  export function parseAliasText(text: string): string[]
  ```

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/columnAssign.test.ts
import { describe, expect, it } from 'vitest'
import {
  assignColumnAsField,
  assignColumnAsKey,
  parseAliasText,
  setFieldAliases,
  setKeyAliases,
} from './columnAssign'
import type { WorkspaceState, WorkspaceTermType } from './types'

const term = (overrides: Partial<WorkspaceTermType> & { value: string }): WorkspaceTermType => ({
  display_name: overrides.value,
  provenance: 'skill',
  review: 'accepted',
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: [],
  field_aliases: {},
  clues: [],
  data_match: null,
  ...overrides,
})

const base = (): WorkspaceState => ({
  term_types: [term({ value: 'SKU', key_aliases: ['jan'] }), term({ value: 'Store', review: 'rejected' })],
  relation_types: [],
  constraints: [],
  sources: [
    {
      file: 'sku.xls',
      columns: [
        { name: '商品コード', role: 'identifier', reason: '每行一个', inferred_type: 'string' },
        { name: '売価', role: 'measure', reason: '带小数', inferred_type: 'number' },
        { name: '色', role: 'dimension', reason: '5 个取值', inferred_type: 'string' },
      ],
    },
  ],
  unmatched_columns: { 'sku.xls': ['商品コード', '売価', '色'] },
  questions: [],
})

describe('assignColumnAsKey', () => {
  it('写 data_match、追加键别名、从未接住里移除', () => {
    const next = assignColumnAsKey(base(), 'sku.xls', '商品コード', 'SKU')
    const sku = next.term_types[0]
    expect(sku.data_match).toEqual({
      source_file: 'sku.xls',
      key_columns: ['商品コード'],
      field_columns: {},
      matched_by: 'manual',
    })
    // 别名追加不覆盖：下次同一客户的表能自动命中，原有的 jan 也还在
    expect(sku.key_aliases).toEqual(['jan', '商品コード'])
    expect(next.unmatched_columns['sku.xls']).toEqual(['売価', '色'])
  })

  it('同一张表已有字段匹配时保留字段，换表时清空字段', () => {
    const withFields = base()
    withFields.term_types[0].data_match = {
      source_file: 'sku.xls',
      key_columns: ['JAN'],
      field_columns: { color: '色' },
      matched_by: 'alias:jan',
    }
    expect(assignColumnAsKey(withFields, 'sku.xls', '商品コード', 'SKU').term_types[0].data_match!.field_columns).toEqual({ color: '色' })
    expect(assignColumnAsKey(withFields, 'other.csv', 'x', 'SKU').term_types[0].data_match!.field_columns).toEqual({})
  })

  it('指给已拒绝或不存在的实体原样返回', () => {
    const s = base()
    expect(assignColumnAsKey(s, 'sku.xls', '商品コード', 'Store')).toBe(s)
    expect(assignColumnAsKey(s, 'sku.xls', '商品コード', 'Nope')).toBe(s)
  })

  it('不改入参', () => {
    const s = base()
    const snapshot = JSON.stringify(s)
    assignColumnAsKey(s, 'sku.xls', '商品コード', 'SKU')
    expect(JSON.stringify(s)).toBe(snapshot)
  })
})

describe('assignColumnAsField', () => {
  const keyed = (): WorkspaceState => assignColumnAsKey(base(), 'sku.xls', '商品コード', 'SKU')

  it('加字段、写 field_columns 与字段别名、value_type 按推断类型', () => {
    const next = assignColumnAsField(keyed(), 'sku.xls', '売価', 'SKU')
    const sku = next.term_types[0]
    // 中文/日文列名清洗后是空壳，回落成 field_N；显示名记原列名
    const field = sku.extra_fields[0]
    expect(field.label).toBe('売価')
    expect(field.value_type).toBe('number')
    expect(sku.data_match!.field_columns[field.name]).toBe('売価')
    expect(sku.field_aliases[field.name]).toEqual(['売価'])
    expect(next.unmatched_columns['sku.xls']).toEqual(['色'])
  })

  it('实体还没在这张表有键列时拒绝（原样返回）', () => {
    const s = base()
    expect(assignColumnAsField(s, 'sku.xls', '売価', 'SKU')).toBe(s)
  })

  it('字段内部名与已有字段撞名时加后缀', () => {
    const s = keyed()
    s.term_types[0].extra_fields = [{ name: 'price', value_type: 'number', label: '价' }]
    s.sources[0].columns!.push({ name: 'price', role: 'measure', reason: '', inferred_type: 'number' })
    s.unmatched_columns['sku.xls'].push('price')
    const next = assignColumnAsField(s, 'sku.xls', 'price', 'SKU')
    expect(next.term_types[0].extra_fields.map((f) => f.name)).toEqual(['price', 'price_2'])
  })
})

describe('别名编辑', () => {
  it('parseAliasText 按中英文逗号拆、去空白、去重、去空', () => {
    expect(parseAliasText(' jan，JAN, sku_code ,, jan ')).toEqual(['jan', 'JAN', 'sku_code'])
    expect(parseAliasText('')).toEqual([])
  })

  it('setKeyAliases 整份替换', () => {
    const next = setKeyAliases(base(), 'SKU', ['sku', '品番'])
    expect(next.term_types[0].key_aliases).toEqual(['sku', '品番'])
  })

  it('setFieldAliases 只改指定字段，字段不存在时原样返回', () => {
    const s = base()
    s.term_types[0].extra_fields = [{ name: 'color', value_type: 'string', label: '颜色' }]
    s.term_types[0].field_aliases = { color: ['色'] }
    expect(setFieldAliases(s, 'SKU', 'color', ['色', 'colour']).term_types[0].field_aliases).toEqual({ color: ['色', 'colour'] })
    expect(setFieldAliases(s, 'SKU', 'size', ['x'])).toBe(s)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/columnAssign.test.ts --maxWorkers=2`
Expected: 找不到模块 `./columnAssign`

- [ ] **Step 3: 实现**

```ts
// frontend/src/admin/modelingWorkbench/columnAssign.ts
import { sanitizeFieldName } from '../guidedOntology/draftProposal'
import type { InferredType } from '../guidedOntology/types'
import type { WorkspaceState, WorkspaceTermType } from './types'

/**
 * 手动指列与别名编辑。全是纯函数：输入非法时**原样返回同一引用**，调用方据此
 * 判断"什么都没变"并提示——跟 skeletonEdits.ts 同一约定。
 *
 * 这是主 spec 行为规格 §3 说的"命不中的代价是用户手动指一下列"的那个入口。
 * 指完把列名写进别名，下一次同一客户的表就能自动命中——手动一次，规则长期有效。
 */

function replaceTerm(
  state: WorkspaceState,
  value: string,
  update: (term: WorkspaceTermType) => WorkspaceTermType,
): WorkspaceState {
  return { ...state, term_types: state.term_types.map((t) => (t.value === value ? update(t) : t)) }
}

function findActive(state: WorkspaceState, termValue: string): WorkspaceTermType | undefined {
  const term = state.term_types.find((t) => t.value === termValue)
  return term && term.review !== 'rejected' ? term : undefined
}

function removeUnmatched(state: WorkspaceState, file: string, column: string): Record<string, string[]> {
  return {
    ...state.unmatched_columns,
    [file]: (state.unmatched_columns[file] ?? []).filter((name) => name !== column),
  }
}

function appendUnique(list: string[], item: string): string[] {
  return list.includes(item) ? list : [...list, item]
}

/**
 * 把一列指给实体当键列。整体替换 data_match 的键，但同一张表上已经对上的字段
 * 保留——用户只是换了键列，没说要放弃字段；换表则字段全部作废（它们是另一张
 * 表的列）。
 */
export function assignColumnAsKey(
  state: WorkspaceState,
  file: string,
  column: string,
  termValue: string,
): WorkspaceState {
  const term = findActive(state, termValue)
  if (!term) return state
  const sameFile = term.data_match?.source_file === file
  return {
    ...replaceTerm(state, termValue, (t) => ({
      ...t,
      key_aliases: appendUnique(t.key_aliases, column),
      data_match: {
        source_file: file,
        key_columns: [column],
        field_columns: sameFile ? { ...t.data_match!.field_columns } : {},
        matched_by: 'manual',
      },
    })),
    unmatched_columns: removeUnmatched(state, file, column),
  }
}

function valueTypeOf(inferred: InferredType | undefined): string {
  if (inferred === 'integer' || inferred === 'number' || inferred === 'date') return inferred
  return 'string'
}

function uniqueFieldName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base
  let n = 2
  while (taken.has(`${base}_${n}`)) n += 1
  return `${base}_${n}`
}

/**
 * 把一列指给实体当字段。要求实体已经在**同一张表**有键列：字段是"这一行的
 * 属性"，没有键就不知道这一行是谁的。
 */
export function assignColumnAsField(
  state: WorkspaceState,
  file: string,
  column: string,
  termValue: string,
): WorkspaceState {
  const term = findActive(state, termValue)
  if (!term || !term.data_match || term.data_match.source_file !== file) return state
  const source = state.sources.find((s) => s.file === file)
  const columnIndex = source?.columns?.findIndex((c) => c.name === column) ?? -1
  const inferred = columnIndex >= 0 ? source!.columns![columnIndex].inferred_type : undefined
  const taken = new Set(term.extra_fields.map((f) => f.name))
  const fieldName = uniqueFieldName(sanitizeFieldName(column, Math.max(columnIndex, 0)), taken)
  return {
    ...replaceTerm(state, termValue, (t) => ({
      ...t,
      // 显示名记原列名：清洗后的内部名对中文列名只是 field_N 这种占位。
      extra_fields: [...t.extra_fields, { name: fieldName, value_type: valueTypeOf(inferred), label: column }],
      field_aliases: { ...t.field_aliases, [fieldName]: [column] },
      data_match: {
        ...t.data_match!,
        field_columns: { ...t.data_match!.field_columns, [fieldName]: column },
      },
    })),
    unmatched_columns: removeUnmatched(state, file, column),
  }
}

export function parseAliasText(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of text.split(/[,，]/)) {
    const alias = raw.trim()
    if (alias === '' || seen.has(alias)) continue
    seen.add(alias)
    out.push(alias)
  }
  return out
}

export function setKeyAliases(state: WorkspaceState, termValue: string, aliases: string[]): WorkspaceState {
  if (!state.term_types.some((t) => t.value === termValue)) return state
  return replaceTerm(state, termValue, (t) => ({ ...t, key_aliases: [...aliases] }))
}

export function setFieldAliases(
  state: WorkspaceState,
  termValue: string,
  fieldName: string,
  aliases: string[],
): WorkspaceState {
  const term = state.term_types.find((t) => t.value === termValue)
  if (!term || !term.extra_fields.some((f) => f.name === fieldName)) return state
  return replaceTerm(state, termValue, (t) => ({
    ...t,
    field_aliases: { ...t.field_aliases, [fieldName]: [...aliases] },
  }))
}
```

- [ ] **Step 4: 跑测试确认通过**

Run 同 Step 2。Expected: 全部 PASS。若 `字段内部名与已有字段撞名` 那条因 `sanitizeFieldName('price', 3)` 返回 `price` 而通过、但 `売価` 那条的 `field.name` 不是 `field_2`——不要紧，测试没断言具体占位名。

- [ ] **Step 5: 变异检查**

1. `assignColumnAsKey` 里 `key_aliases: appendUnique(...)` 改成 `key_aliases: t.key_aliases`：`写 data_match、追加键别名` 必须变红。改回。
2. `assignColumnAsField` 开头的 `term.data_match.source_file !== file` 条件删掉：`实体还没在这张表有键列时拒绝` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/columnAssign.ts frontend/src/admin/modelingWorkbench/columnAssign.test.ts
git commit -m "feat(admin): 手动指列与别名编辑的纯函数

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: 数据面板——角色/依据显示 + 「指给…」

**Files:**
- Modify: `frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx`（未接住列那一节）
- Modify: `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`（新回调 `handleAssign`）
- Test: `frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx`（追加两条）

**Interfaces:**
- Consumes: Task 2 的 `assignColumnAsKey` / `assignColumnAsField`；Task 1 的 `SourceColumn`
- Produces: `DataPanel` 新 prop `onAssign: (file: string, column: string, termValue: string, as: 'key' | 'field') => void`

- [ ] **Step 1: 写失败的测试**

追加到 `workbenchPage.test.tsx`。需要一个带未接住列的夹具：在文件里加一个 `workspaceWithUnmatched()`，基于 `workspaceWith('accepted')`，把 `state.sources` 设为
`[{ file: 'sku.xls', columns: [{ name: '商品コード', role: 'identifier', reason: '25 个非空值里 25 个不同（100%）', inferred_type: 'string' }, { name: '売価', role: 'measure', reason: '带小数', inferred_type: 'number' }] }]`，
`state.unmatched_columns` 设为 `{ 'sku.xls': ['商品コード', '売価'] }`。

```tsx
  it('未接住的列旁显示角色和判定依据', async () => {
    signedInRole = 'member'
    workspace = workspaceWithUnmatched()
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '数据' }))
    expect(await screen.findByText('商品コード')).toBeInTheDocument()
    // 依据要能看见——用户据此判断该不该指给 SKU
    expect(screen.getByText(/25 个非空值里 25 个不同/)).toBeInTheDocument()
    expect(screen.getByText('标识')).toBeInTheDocument()
  })

  it('把一列指给 SKU 当键列后整份存回，别名跟着写进去', async () => {
    signedInRole = 'member'
    workspace = workspaceWithUnmatched()
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '数据' }))
    await userEvent.selectOptions(await screen.findByLabelText('把 商品コード 指给'), 'SKU:key')
    await userEvent.click(screen.getByRole('button', { name: '指给 商品コード' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as {
      state: { term_types: { value: string; key_aliases: string[]; data_match: { key_columns: string[]; matched_by: string } | null }[]; unmatched_columns: Record<string, string[]> }
    }
    const sku = body.state.term_types.find((t) => t.value === 'SKU')!
    expect(sku.data_match?.key_columns).toEqual(['商品コード'])
    expect(sku.data_match?.matched_by).toBe('manual')
    expect(sku.key_aliases).toContain('商品コード')
    expect(body.state.unmatched_columns['sku.xls']).toEqual(['売価'])
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run src/admin/modelingWorkbench/workbenchPage.test.tsx --maxWorkers=2 -t "指给|判定依据"`
Expected: 两条 FAIL（找不到文本/控件）

- [ ] **Step 3: 改 DataPanel**

props 加 `onAssign`。未接住列那一节改成每列一行。在组件顶部加本地草稿 `const [assignDraft, setAssignDraft] = useState<Record<string, string>>({})`（键是 `${file}::${column}`，值是 `${termValue}:${'key'|'field'}`）。加两个小工具：

```tsx
const ROLE_LABEL: Record<string, string> = {
  identifier: '标识',
  dimension: '维度',
  measure: '度量',
  freetext: '文本',
  date: '日期',
}

function columnInfo(state: WorkspaceState, file: string, column: string) {
  return state.sources.find((s) => s.file === file)?.columns?.find((c) => c.name === column)
}
```

渲染（替换原来 `columns.map(... 提升为实体类型 ...)` 那段）：

```tsx
            <div className="flex flex-col gap-2">
              {columns.map((column) => {
                const info = columnInfo(props.state, fileName, column)
                const draftKey = `${fileName}::${column}`
                const candidates = props.state.term_types.filter((t) => t.review !== 'rejected')
                return (
                  <div key={column} className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-ink">{column}</span>
                    {info && <span className={tagClass}>{ROLE_LABEL[info.role] ?? info.role}</span>}
                    {info && <span className="text-xs text-ink-soft">{info.reason}</span>}
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={props.busy}
                      onClick={() => props.onPromote(fileName, column)}
                    >
                      {`把 ${column} 提升为实体类型`}
                    </button>
                    <label className="sr-only" htmlFor={`assign-${draftKey}`}>
                      {`把 ${column} 指给`}
                    </label>
                    <select
                      id={`assign-${draftKey}`}
                      className="rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
                      value={assignDraft[draftKey] ?? ''}
                      onChange={(e) => setAssignDraft({ ...assignDraft, [draftKey]: e.target.value })}
                    >
                      <option value="">指给…</option>
                      {candidates.map((t) => (
                        <optgroup key={t.value} label={t.value}>
                          <option value={`${t.value}:key`}>{`改为用 ${column} 当 ${t.value} 的键列`}</option>
                          <option value={`${t.value}:field`}>{`当 ${t.value} 的字段`}</option>
                        </optgroup>
                      ))}
                    </select>
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={props.busy || !assignDraft[draftKey]}
                      onClick={() => {
                        const [termValue, as] = (assignDraft[draftKey] ?? '').split(':')
                        if (!termValue || (as !== 'key' && as !== 'field')) return
                        props.onAssign(fileName, column, termValue, as)
                        setAssignDraft({ ...assignDraft, [draftKey]: '' })
                      }}
                    >
                      {`指给 ${column}`}
                    </button>
                  </div>
                )
              })}
            </div>
```

- [ ] **Step 4: 页面回调**

`ModelingWorkbenchPage.tsx` 加 import `assignColumnAsField, assignColumnAsKey` from `'./columnAssign'`，`handlePromote` 之后加：

```tsx
  const handleAssign = (file: string, column: string, termValue: string, as: 'key' | 'field') => {
    if (!workspace) return
    const next =
      as === 'key'
        ? assignColumnAsKey(workspace.state, file, column, termValue)
        : assignColumnAsField(workspace.state, file, column, termValue)
    if (next === workspace.state) {
      setError(
        as === 'field'
          ? `${termValue} 还没在 ${file} 里有键列，先把它的键列指到这张表。`
          : `没法把 ${column} 指给 ${termValue}：它不存在或已被拒绝。`,
      )
      return
    }
    void persist(next)
  }
```

`<DataPanel …>` 加 `onAssign={handleAssign}`。

- [ ] **Step 5: 跑测试确认通过**

Run 同 Step 2，再跑整个 `src/admin/modelingWorkbench`。Expected: 全部 PASS

- [ ] **Step 6: 变异检查**

把 `handleAssign` 里 `as === 'key' ? assignColumnAsKey(...) : ...` 改成恒走 `assignColumnAsField`：`把一列指给 SKU 当键列后整份存回` 必须变红（字段路径会因没有键而原样返回、不保存）。改回。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
git commit -m "feat(admin): 数据面板显示列角色与依据，未接住的列可手动指给实体

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: 骨架面板——别名编辑

**Files:**
- Modify: `frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx`
- Modify: `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`
- Test: `frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx`（追加两条）

**Interfaces:**
- Consumes: Task 2 的 `setKeyAliases` / `setFieldAliases` / `parseAliasText`
- Produces: `SkeletonPanel` 新 props `onSetKeyAliases(termValue, aliases: string[])`、`onSetFieldAliases(termValue, fieldName, aliases: string[])`

- [ ] **Step 1: 写失败的测试**

```tsx
  it('改键别名后整份存回', async () => {
    signedInRole = 'member'
    workspace = workspaceWith('accepted')
    renderWorkbench()
    const input = await screen.findByLabelText('SKU 的键别名')
    await userEvent.clear(input)
    await userEvent.type(input, 'jan, 品番，JAN_CD')
    await userEvent.click(screen.getByRole('button', { name: '保存 SKU 的键别名' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { key_aliases: string[] }[] } }
    expect(body.state.term_types[0].key_aliases).toEqual(['jan', '品番', 'JAN_CD'])
  })

  it('每个字段有自己的别名框', async () => {
    signedInRole = 'member'
    const ws = workspaceWith('accepted')
    ws.state.term_types[0].extra_fields = [{ name: 'color', value_type: 'string', label: '颜色' }]
    ws.state.term_types[0].field_aliases = { color: ['色'] }
    workspace = ws
    renderWorkbench()
    const input = await screen.findByLabelText('SKU 的字段 color 的别名')
    expect(input).toHaveValue('色')
    await userEvent.type(input, ', colour')
    await userEvent.click(screen.getByRole('button', { name: '保存 SKU 的字段 color 的别名' }))
    await waitFor(() => expect(saved).toHaveLength(1))
    const body = saved[0] as { state: { term_types: { field_aliases: Record<string, string[]> }[] } }
    expect(body.state.term_types[0].field_aliases.color).toEqual(['色', 'colour'])
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `... workbenchPage.test.tsx --maxWorkers=2 -t "别名"`。Expected: FAIL

- [ ] **Step 3: 改 SkeletonPanel**

props 加两个回调；加本地草稿 `const [aliasDraft, setAliasDraft] = useState<Record<string, string>>({})`（键 `key:${value}` 或 `field:${value}:${fieldName}`）。在每个实体行的控件之后（改名/旁证之后）追加一块：

```tsx
            <div className="flex w-full flex-wrap items-center gap-2 pl-4">
              <label className="sr-only" htmlFor={`alias-key-${term.value}`}>{`${term.value} 的键别名`}</label>
              <input
                id={`alias-key-${term.value}`}
                className="w-64 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
                placeholder="键别名，逗号分隔"
                value={aliasDraft[`key:${term.value}`] ?? term.key_aliases.join(', ')}
                onChange={(e) => setAliasDraft({ ...aliasDraft, [`key:${term.value}`]: e.target.value })}
              />
              <button
                type="button"
                className={secondaryButtonClass}
                disabled={props.busy}
                onClick={() =>
                  props.onSetKeyAliases(
                    term.value,
                    parseAliasText(aliasDraft[`key:${term.value}`] ?? term.key_aliases.join(', ')),
                  )
                }
              >
                {`保存 ${term.value} 的键别名`}
              </button>
              {term.extra_fields.map((field) => {
                const draftKey = `field:${term.value}:${field.name}`
                const current = (term.field_aliases[field.name] ?? []).join(', ')
                return (
                  <span key={field.name} className="flex items-center gap-2">
                    <label className="sr-only" htmlFor={`alias-${draftKey}`}>
                      {`${term.value} 的字段 ${field.name} 的别名`}
                    </label>
                    <input
                      id={`alias-${draftKey}`}
                      className="w-48 rounded-control border border-subtle bg-paper px-2 py-1 text-sm"
                      placeholder={`${field.label || field.name} 的别名`}
                      value={aliasDraft[draftKey] ?? current}
                      onChange={(e) => setAliasDraft({ ...aliasDraft, [draftKey]: e.target.value })}
                    />
                    <button
                      type="button"
                      className={secondaryButtonClass}
                      disabled={props.busy}
                      onClick={() =>
                        props.onSetFieldAliases(term.value, field.name, parseAliasText(aliasDraft[draftKey] ?? current))
                      }
                    >
                      {`保存 ${term.value} 的字段 ${field.name} 的别名`}
                    </button>
                  </span>
                )
              })}
              <span className="text-xs text-ink-soft">别名改了要重新扫描数据表才生效。</span>
            </div>
```

import `parseAliasText` from `'../columnAssign'`。

- [ ] **Step 4: 页面回调**

```tsx
  const handleSetKeyAliases = (termValue: string, aliases: string[]) => {
    if (!workspace) return
    const next = setKeyAliases(workspace.state, termValue, aliases)
    if (next !== workspace.state) void persist(next)
  }

  const handleSetFieldAliases = (termValue: string, fieldName: string, aliases: string[]) => {
    if (!workspace) return
    const next = setFieldAliases(workspace.state, termValue, fieldName, aliases)
    if (next !== workspace.state) void persist(next)
  }
```

import 并把两个回调传给 `<SkeletonPanel>`。

- [ ] **Step 5: 跑测试确认通过**；**Step 6: 变异**：把 `parseAliasText` 调用换成 `[text]`（不拆分）——`改键别名后整份存回` 必须变红。改回。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/panels/SkeletonPanel.tsx frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
git commit -m "feat(admin): 骨架面板可直接编辑键别名与字段别名

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 关系映射的列存在性检查

**Files:**
- Modify: `frontend/src/admin/modelingWorkbench/types.ts`（加 `SkippedRelation`）
- Modify: `frontend/src/admin/modelingWorkbench/projectToDraft.ts`
- Modify: `frontend/src/admin/modelingWorkbench/panels/ApplyPanel.tsx`
- Modify: `frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx`
- Test: `frontend/src/admin/modelingWorkbench/projectToDraft.test.ts`（追加）、`workbenchPage.test.tsx`（追加一条）

**Interfaces:**
- Produces:
  ```ts
  export interface SkippedRelation { subject: string; relation: string; object: string; reason: string }
  export function projectToEtlYaml(state, tenantId): { yaml: string; fileName: string; skippedRelations: SkippedRelation[] } | null
  export function previewSkippedRelations(state): SkippedRelation[]   // 供应用面板在预览时就显示
  ```
- `ApplyPanel` 新 prop `skippedRelations: SkippedRelation[]`

- [ ] **Step 1: 写失败的测试**（追加到 `projectToDraft.test.ts`）

```ts
describe('关系映射的列检查', () => {
  const store = term({
    value: 'Store',
    data_match: { source_file: 'store.csv', key_columns: ['STORE_CD'], field_columns: {}, matched_by: 'alias:store_cd' },
  })
  const withStore = (subjectColumns: string[] | undefined): WorkspaceState =>
    state({
      term_types: [SKU, store],
      constraints: [
        { subject: 'SKU', relation: 'SOLD_AT', object: 'Store', provenance: 'skill', review: 'pending' },
      ],
      sources: [
        { file: 'sku.xls', header_row: 6, ...(subjectColumns ? { columns: subjectColumns.map((name) => ({ name, role: 'dimension' as const, reason: '', inferred_type: 'string' as const })) } : {}) },
        { file: 'store.csv', columns: [{ name: 'STORE_CD', role: 'identifier', reason: '', inferred_type: 'string' }] },
      ],
    })

  it('主语表里有宾语键列时出关系映射', () => {
    const built = projectToEtlYaml(withStore(['JAN', 'STORE_CD']), 't1')!
    expect(built.yaml).toContain('relation_type: "SOLD_AT"')
    expect(built.skippedRelations).toEqual([])
  })

  it('主语表里没有宾语键列时不出关系，并说明原因', () => {
    // ETL 的关系语义是同一行用宾语的 node_key 列算宾语键；列不在就整条跳过且不报错
    const built = projectToEtlYaml(withStore(['JAN', 'cat_cd']), 't1')!
    expect(built.yaml).not.toContain('SOLD_AT')
    expect(built.skippedRelations).toEqual([
      { subject: 'SKU', relation: 'SOLD_AT', object: 'Store', reason: '主语表 sku.xls 里没有 Store 的键列 STORE_CD' },
    ])
  })

  it('主语表没有 columns（v1 旧工作区）时视为未知，跳过并说明', () => {
    const built = projectToEtlYaml(withStore(undefined), 't1')!
    expect(built.skippedRelations[0].reason).toContain('还没重新扫描过')
  })

  it('previewSkippedRelations 与投影结果一致', () => {
    expect(previewSkippedRelations(withStore(['JAN', 'cat_cd']))).toHaveLength(1)
    expect(previewSkippedRelations(withStore(['JAN', 'STORE_CD']))).toEqual([])
  })
})
```

（`term` / `SKU` / `state` 是该测试文件已有的夹具；`SKU.data_match.source_file` 是 `sku.xls`。）

页面测试追加一条：

```tsx
  it('应用面板列出出不了关系映射的约束', async () => {
    signedInRole = 'member'
    const ws = workspaceWith('accepted')
    ws.state.term_types[0].data_match = { source_file: 'sku.xls', key_columns: ['JAN'], field_columns: {}, matched_by: 'alias:jan' }
    ws.state.term_types.push({ ...ws.state.term_types[0], value: 'Store', display_name: 'Store', data_match: { source_file: 'store.csv', key_columns: ['STORE_CD'], field_columns: {}, matched_by: 'alias:store_cd' } })
    ws.state.constraints = [{ subject: 'SKU', relation: 'SOLD_AT', object: 'Store', provenance: 'skill', review: 'pending' }]
    ws.state.sources = [{ file: 'sku.xls', columns: [{ name: 'JAN', role: 'identifier', reason: '', inferred_type: 'string' }] }]
    workspace = ws
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: /^应用$/ }))
    await userEvent.click(await screen.findByRole('button', { name: /看看会改什么/ }))
    expect(await screen.findByText(/没有 Store 的键列 STORE_CD/)).toBeInTheDocument()
    expect(screen.getByText(/表格导入页手动配/)).toBeInTheDocument()
  })
```

- [ ] **Step 2: 跑测试确认失败**（`projectToDraft.test.ts` 编译失败：`skippedRelations` 不存在）

- [ ] **Step 3: 实现**

`types.ts`：

```ts
export interface SkippedRelation {
  subject: string
  relation: string
  object: string
  reason: string
}
```

`projectToDraft.ts`：把 relations 那段替换为

```ts
  const { relations, skipped } = pickRelations(state, matched)
  return {
    yaml: buildConfigYaml({ tenantId, entities, relations, files }),
    fileName: fileNames[0],
    skippedRelations: skipped,
  }
```

并新增：

```ts
/**
 * 决定哪些约束能出关系映射。
 *
 * 既有 ETL 的关系语义：从主语表出，**同一行**用宾语实体的 node_key 列算宾语键
 * （etl_projection 不接受"宾语键在这张表叫别的名字"）。所以宾语的键列必须原名
 * 出现在主语表里，否则每一行都因缺列变成 RowFailure——整条关系静默跳过，配置层
 * 不报错。这里提前判掉，把原因交给应用面板说出来，比让用户跑完批才发现强。
 *
 * 主语表没有 columns（v1 存下的旧工作区）时视为未知，同样跳过：宁可让用户重扫
 * 一次，也不出一条可能整条跑空的映射。
 */
function pickRelations(
  state: WorkspaceState,
  matched: WorkspaceTermType[],
): { relations: BuilderRelation[]; skipped: SkippedRelation[] } {
  const relations: BuilderRelation[] = []
  const skipped: SkippedRelation[] = []
  for (const c of state.constraints) {
    if (c.review === 'rejected') continue
    const subject = matched.find((t) => t.value === c.subject)
    const object = matched.find((t) => t.value === c.object)
    if (!subject || !object) continue
    const subjectFile = subject.data_match!.source_file
    const columns = state.sources.find((s) => s.file === subjectFile)?.columns
    const entry = { subject: c.subject, relation: c.relation, object: c.object }
    if (columns === undefined) {
      skipped.push({ ...entry, reason: `主语表 ${subjectFile} 还没重新扫描过，不知道有哪些列` })
      continue
    }
    const names = new Set(columns.map((col) => col.name))
    const missing = object.data_match!.key_columns.filter((k) => !names.has(k))
    if (missing.length > 0) {
      skipped.push({ ...entry, reason: `主语表 ${subjectFile} 里没有 ${c.object} 的键列 ${missing.join('/')}` })
      continue
    }
    relations.push({
      id: `${c.subject}-${c.relation}-${c.object}`,
      fileId: subjectFile,
      subjectTermType: c.subject,
      relationType: c.relation,
      objectTermType: c.object,
    })
  }
  return { relations, skipped }
}

export function previewSkippedRelations(state: WorkspaceState): SkippedRelation[] {
  const matched = state.term_types.filter((t) => t.review === 'accepted' && t.data_match !== null)
  return pickRelations(state, matched).skipped
}
```

`ApplyPanel.tsx`：props 加 `skippedRelations: SkippedRelation[]`，在 diff 区块之后渲染：

```tsx
      {props.skippedRelations.length > 0 && (
        <div className="flex flex-col gap-1 text-sm text-ink">
          <p className="font-bold">这些关系出不了映射，要在表格导入页手动配：</p>
          {props.skippedRelations.map((s) => (
            <p key={`${s.subject}-${s.relation}-${s.object}`}>{`${s.subject} -${s.relation}-> ${s.object}：${s.reason}`}</p>
          ))}
        </div>
      )}
```

`ModelingWorkbenchPage.tsx`：`<ApplyPanel skippedRelations={workspace ? previewSkippedRelations(workspace.state) : []} …/>`。`handleApply` 里 `mapping.yaml/fileName` 的用法不变。

- [ ] **Step 4: 跑测试确认通过**；**Step 5: 变异**：`pickRelations` 里 `missing.length > 0` 改成 `false`——`主语表里没有宾语键列时不出关系` 必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/types.ts frontend/src/admin/modelingWorkbench/projectToDraft.ts frontend/src/admin/modelingWorkbench/panels/ApplyPanel.tsx frontend/src/admin/modelingWorkbench/ModelingWorkbenchPage.tsx frontend/src/admin/modelingWorkbench/projectToDraft.test.ts frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
git commit -m "feat(admin): 投影关系映射前检查宾语键列是否在主语表，出不了的在应用面板说明

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: 空白起步退化路径

**Files:**
- Create: `frontend/src/admin/modelingWorkbench/blankStart.ts`
- Modify: `frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx`（`handleScan`）
- Test: `frontend/src/admin/modelingWorkbench/blankStart.test.ts`、`workbenchPage.test.tsx`（追加一条）

**Interfaces:**
- Consumes: `guidedOntology/draftProposal.ts` 的 `buildProposal(roled, decision): Proposal`、`initialDecision(roled)`；`Proposal.termTypes[i].extra_fields[j].label` 是原列名
- Produces: `proposalToWorkspace(proposal: Proposal, file: string): Pick<WorkspaceState, 'term_types' | 'relation_types' | 'constraints'> & { unmatched: string[] }`

- [ ] **Step 1: 写失败的测试**

```ts
// frontend/src/admin/modelingWorkbench/blankStart.test.ts
import { describe, expect, it } from 'vitest'
import type { Proposal } from '../guidedOntology/types'
import { proposalToWorkspace } from './blankStart'

const proposal: Proposal = {
  termTypes: [
    {
      value: '订单号',
      extra_fields: [{ name: 'revenue', value_type: 'number', label: 'revenue' }, { name: 'field_3', value_type: 'date', label: '下单日期' }],
      standard_name_value_type: 'string',
    },
    { value: '产品', extra_fields: [], standard_name_value_type: 'string' },
  ],
  relationTypes: [{ relation_type: 'HAS_PRODUCT', example_phrase: '', description: '', allow_chain_query: true }],
  constraints: [{ subject_term_type: '订单号', relation_type: 'HAS_PRODUCT', object_term_type: '产品' }],
  unusedColumns: ['备注'],
  attributeColumns: ['revenue', '下单日期'],
  renamedFields: { 下单日期: 'field_3' },
  collidedFields: [],
  rootIsGuessed: false,
  rootName: '订单号',
  reparentedTo: { root: '订单号', names: [] },
}

describe('proposalToWorkspace', () => {
  it('实体带来源 data、待审、键别名是列名、data_match 用 column_role', () => {
    const out = proposalToWorkspace(proposal, 'orders.csv')
    const order = out.term_types.find((t) => t.value === '订单号')!
    expect(order.provenance).toBe('data')
    expect(order.review).toBe('pending')
    expect(order.key_aliases).toEqual(['订单号'])
    expect(order.data_match).toEqual({
      source_file: 'orders.csv',
      key_columns: ['订单号'],
      // 字段名 → 原列名：label 记的就是原列名
      field_columns: { revenue: 'revenue', field_3: '下单日期' },
      matched_by: 'column_role',
    })
    expect(order.field_aliases).toEqual({ revenue: ['revenue'], field_3: ['下单日期'] })
    expect(order.extra_fields).toEqual(proposal.termTypes[0].extra_fields)
  })

  it('关系与约束也是 data/pending', () => {
    const out = proposalToWorkspace(proposal, 'orders.csv')
    expect(out.relation_types[0]).toMatchObject({ relation_type: 'HAS_PRODUCT', provenance: 'data', review: 'pending', clues: [], data_match: null })
    expect(out.constraints[0]).toEqual({ subject: '订单号', relation: 'HAS_PRODUCT', object: '产品', provenance: 'data', review: 'pending' })
  })

  it('没用上的列交回去当未接住列', () => {
    expect(proposalToWorkspace(proposal, 'orders.csv').unmatched).toEqual(['备注'])
  })
})
```

页面测试（用 Task 1 提到的 `csvFile()` 夹具，工作区用**空白起步**——`skill_name: null`、`term_types: []`）：

```tsx
  it('空白起步传表后推出一套待审骨架', async () => {
    signedInRole = 'member'
    const ws = workspaceWith('accepted')
    ws.skill_name = null
    ws.state.term_types = []
    ws.state.relation_types = []
    ws.state.constraints = []
    workspace = ws
    renderWorkbench()
    await userEvent.click(await screen.findByRole('button', { name: '数据' }))
    await userEvent.upload(screen.getByLabelText('选择数据表'), csvFile())
    await userEvent.click(await screen.findByRole('button', { name: '扫描并对齐' }))
    await waitFor(() => expect(saved.length).toBeGreaterThan(0))
    const body = saved[saved.length - 1] as { state: { term_types: { value: string; provenance: string; review: string }[]; constraints: unknown[] } }
    const values = body.state.term_types.map((t) => t.value)
    expect(values).toContain('订单号')
    expect(values).toContain('产品')
    expect(body.state.term_types.every((t) => t.provenance === 'data' && t.review === 'pending')).toBe(true)
    expect(body.state.constraints.length).toBeGreaterThan(0)
  })
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现**

```ts
// frontend/src/admin/modelingWorkbench/blankStart.ts
import type { Proposal } from '../guidedOntology/types'
import type { WorkspaceConstraint, WorkspaceRelationType, WorkspaceTermType } from './types'

/**
 * 空白起步 + 只传表的退化路径（主 spec 前端一节承诺保留的那条）。
 *
 * 把 buildProposal 推出来的骨架翻成工作区元素。全部 provenance=data、review=
 * pending：这是"数据说这里可能有个概念"，不是用户的决定，用户仍在骨架面板逐条审。
 * 键别名取列名本身、字段别名取原列名（label）：下次同一客户再传同构的表能自动
 * 命中，跟手动指列的做法一致。
 */
export function proposalToWorkspace(
  proposal: Proposal,
  file: string,
): { term_types: WorkspaceTermType[]; relation_types: WorkspaceRelationType[]; constraints: WorkspaceConstraint[]; unmatched: string[] } {
  const term_types: WorkspaceTermType[] = proposal.termTypes.map((t) => {
    const fieldColumns = Object.fromEntries(t.extra_fields.map((f) => [f.name, f.label ?? f.name]))
    return {
      value: t.value,
      display_name: t.value,
      provenance: 'data',
      review: 'pending',
      standard_name_value_type: t.standard_name_value_type,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      key_aliases: [t.value],
      field_aliases: Object.fromEntries(Object.entries(fieldColumns).map(([name, column]) => [name, [column]])),
      clues: [],
      data_match: { source_file: file, key_columns: [t.value], field_columns: fieldColumns, matched_by: 'column_role' },
    }
  })
  const relation_types: WorkspaceRelationType[] = proposal.relationTypes.map((r) => ({
    relation_type: r.relation_type,
    example_phrase: r.example_phrase,
    description: r.description,
    provenance: 'data',
    review: 'pending',
    clues: [],
    data_match: null,
  }))
  const constraints: WorkspaceConstraint[] = proposal.constraints.map((c) => ({
    subject: c.subject_term_type,
    relation: c.relation_type,
    object: c.object_term_type,
    provenance: 'data',
    review: 'pending',
  }))
  return { term_types, relation_types, constraints, unmatched: [...proposal.unusedColumns] }
}
```

`DataPanel.tsx` `handleScan`：在算出 `table` 之后、`mergeAlignments` 之前加分支：

```tsx
      if (props.state.term_types.length === 0) {
        // 骨架为空（空白起步，或 skill 起步后把骨架删光了）：对齐无从谈起，改走
        // 单表推导——这是被下线的引导页留下的那条路。
        const proposal = buildProposal(table.roled, initialDecision(table.roled))
        const derived = proposalToWorkspace(proposal, file.name)
        props.onMerged({
          ...withSource,
          term_types: derived.term_types,
          relation_types: derived.relation_types,
          constraints: derived.constraints,
          unmatched_columns: { ...withSource.unmatched_columns, [file.name]: derived.unmatched },
        })
        return
      }
```

import `buildProposal, initialDecision` from `'../../guidedOntology/draftProposal'`，`proposalToWorkspace` from `'../blankStart'`。

- [ ] **Step 4: 跑测试确认通过**；**Step 5: 变异**：把分支条件 `props.state.term_types.length === 0` 改成 `false`——页面那条必须变红。改回。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/admin/modelingWorkbench/blankStart.ts frontend/src/admin/modelingWorkbench/blankStart.test.ts frontend/src/admin/modelingWorkbench/panels/DataPanel.tsx frontend/src/admin/modelingWorkbench/workbenchPage.test.tsx
git commit -m "feat(admin): 空白起步传表后用单表推导出一套待审骨架

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: 过期 docstring 与全量回归

**Files:**
- Modify: `frontend/src/admin/guidedOntology/columnRoles.ts:25-27`

- [ ] **Step 1**：把 `INTEGER_IDENTIFIER_MIN_ROWS` 上方"审阅视图现在会把标识列连同 reason 一起展示、并且能改判成属性"那几句改成如实描述：工作台的数据面板会把**未接住**的列连同角色和 reason 一起展示，用户可以把它提升为实体或指给已有实体；已命中的列不再显示 reason。只改注释。
- [ ] **Step 2**：前端全量 `NODE_OPTIONS="--max-old-space-size=4096" npx vitest run --maxWorkers=2` 全绿；`npx tsc --noEmit` 无错。后端 `PYTHONIOENCODING=utf-8 python -u -m pytest tests/ -q` 全绿。
- [ ] **Step 3**：提交
```bash
git add frontend/src/admin/guidedOntology/columnRoles.ts
git commit -m "docs(admin): 列角色说明改成工作台里的实际去向

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
- [ ] **Step 4（人工，用户在场）**：MUJI 表验收：空白起步传表看推导；skill 起步传表后把 `商品コード` 指给 SKU 当键、`売価` 当字段；改 SKU 键别名后重扫命中；两张表时应用面板看关系说明。不点「开始导入」。

## 完成判据
- 上述 7 个 commit；前后端全量全绿。
- 未接住列旁能看到角色与依据，能指给实体当键/当字段，指完别名进工作区。
- 骨架面板能改别名。
- 应用面板对出不了映射的关系逐条说明。
- 空白起步传表后得到一套 `data`/`pending` 的骨架。
