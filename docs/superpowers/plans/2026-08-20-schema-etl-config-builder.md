# Schema ETL 配置构建向导 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在"结构化数据加工"页面新增一个"配置构建向导"——用户对着自己真实数据文件的列，通过界面点选配出 `config.yaml`，不需要手写 YAML，最终复用现有的批量导入提交流程。

**Architecture:** 纯前端功能，不新增/修改任何后端接口。新建一组独立文件（`frontend/src/admin/schemaEtlConfigBuilder/` 目录）：一个无 React 依赖的纯逻辑模块（类型定义 + CSV 表头读取 + YAML 文本生成），两个"单条映射编辑器"展示组件（实体映射、关系映射各一个），一个编排组件把两者和"添加文件"/"拉取已确认本体"/"YAML 预览"/"提交"串起来。编排组件作为一个新的可折叠区块插入 `SchemaEtlPage.tsx`，位于"查看示例数据"区块和现有裸文件上传表单之间。

**Tech Stack:** React + TypeScript，浏览器原生 `File`/`Blob` API（`file.slice().text()` 读表头），不引入任何新 npm 依赖（不用 `js-yaml`，手写 YAML 文本生成）。

**Spec:** `docs/superpowers/specs/2026-08-20-schema-etl-config-builder-design.md`

## 已核实的现状（写任务前的调研结论，供所有任务参考）

- 本体数据源（无需改动，直接消费）：
  - `GET /api/admin/ontology/{tenant_id}/term-types?status=confirmed` → `{term_types: [{value, extra_fields: [{name, value_type}]}]}`（`app/api/admin_ontology_routes.py:66-81`）。
  - `GET /api/admin/ontology/{tenant_id}/constraints?status=confirmed` → `{constraints: [{subject_term_type, relation_type, object_term_type}]}`（同文件 337-352 行）。
  - 两者都走 `Depends(deps.require_admin_session)`，跟页面里已有的 `adminFetch(path, sessionToken)` 调用方式完全一致（`frontend/src/admin/OntologySchemaPage.tsx:131-141` 已经在用同样的路径和 `Constraint`/`TermType` 形状，本计划复用同款字段命名，不重新发明）。
- 提交入口（无需改动，直接复用）：`POST /api/admin/{tenant_id}/schema-etl/runs`，`multipart/form-data`，字段名 `config`（单个文件）+ `data_files`（可重复 append 多个文件），见现有 `SchemaEtlPage.tsx:194-224`（`handleUpload` 函数）里 `formData.append('config', configFile)` / `formData.append('data_files', file)` 的写法——本计划生成的 `Blob` 和已添加的 `File` 对象要按同样的字段名塞进同一个 `FormData` 提交。
- YAML 目标格式（`app/graphrag/schema_etl_config.py`）：
  ```
  tenant_id: <str>
  entities:
    - term_type: <str>
      source_file: <str>
      standard_name_column: <str>
      node_key_parts:
        - column: <str>              # ColumnNodeKeyPart，或：
        - allocated_code:
            scope_columns: [<str>, ...]
            raw_value_column: <str>  # AllocatedCodeNodeKeyPart
      field_mappings:
        <本体字段名>: <CSV列名>
        ...
  relations:
    - relation_type: <str>
      source_file: <str>
      subject_term_type: <str>
      object_term_type: <str>
  ```
- `crypto.randomUUID()` 是本代码库已有的 id 生成惯用法（`frontend/src/hooks/useAgentChat.ts:44`、`frontend/src/lib/identity.ts:12`），本计划的 React key/内部 id 沿用同一写法。
- `SchemaEtlPage.tsx` 当前第 354 行是"查看示例数据"折叠区块的收尾 `</div>`，第 356 行是现有裸上传 `<form onSubmit={handleUpload}...`——新区块插入在这两行之间。

## Global Constraints

- 不新增/修改任何后端文件——本计划只创建/修改 `frontend/` 下的文件。
- 不引入任何新的 npm 依赖（不装 `js-yaml` 等），YAML 生成用手写字符串拼接。
- 前端没有自动化测试框架，每个任务的验证手段只有 `cd frontend && npx tsc --noEmit`——不要在任务里安排"写单元测试"的步骤。**但状态管理/逻辑正确性必须靠仔细手工走查**（`tsc` 只能保证类型对，不能保证 `useEffect` 依赖数组、状态更新时机等运行时逻辑正确——上一个 SDD 任务里就出现过一个 `tsc` 通过但运行时会自我取消请求的 `useEffect` bug，这次每个含 state/effect 逻辑的任务在自查阶段都必须手动逐行走查执行顺序，不能只看 `tsc` 绿了就算数）。
- 所有新增字符串字段（`term_type`、列名、`relation_type`、`tenant_id`）生成到 YAML 里时一律用双引号包裹并转义，不使用无引号的 plain scalar 风格——避免中文/特殊字符触发 YAML 语法歧义。
- 保留现有裸文件上传表单不动，新向导是并列的新增区块，不替换、不删除现有表单。
- 视觉风格必须匹配 `SchemaEtlPage.tsx` 已有的 Tailwind 用法：`border-2 border-ink`、`shadow-brutal`/`shadow-brutal-sm`、`bg-card`/`bg-paper`、主操作用 `bg-accent-pink`、统一的 `focusRing` 常量、`min-h-[44px]` 触达尺寸、`disabled:cursor-not-allowed disabled:opacity-50`。

---

### Task 1: 纯逻辑模块——类型定义、CSV 表头读取、YAML 生成

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/types.ts`
- Create: `frontend/src/admin/schemaEtlConfigBuilder/csvHeader.ts`
- Create: `frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts`

**Interfaces:**
- Consumes：无（本任务是最底层，不依赖其他任务的产出）。
- Produces：
  - `types.ts` 导出：`ExtraFieldSpec`、`ConfirmedTermType`、`ConfirmedCombination`、`AddedFile`、`ColumnKeyPart`、`AllocatedCodeKeyPart`、`NodeKeyPart`（联合类型）、`BuilderEntity`、`BuilderRelation`——Task 2/3/4 都要 import 这些类型，字段名必须逐字匹配下面的代码。
  - `csvHeader.ts` 导出：`readCsvHeaderColumns(file: File): Promise<string[]>`——Task 4 调用它。
  - `buildConfigYaml.ts` 导出：`buildConfigYaml(params: { tenantId: string; entities: BuilderEntity[]; relations: BuilderRelation[]; files: AddedFile[] }): string`——Task 4 调用它生成预览/提交用的 YAML 文本。

- [ ] **Step 1: 创建 `types.ts`**

```typescript
export interface ExtraFieldSpec {
  name: string
  value_type: string
}

export interface ConfirmedTermType {
  value: string
  extra_fields: ExtraFieldSpec[]
}

export interface ConfirmedCombination {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface AddedFile {
  id: string
  file: File
  columns: string[]
}

export interface ColumnKeyPart {
  kind: 'column'
  column: string
}

export interface AllocatedCodeKeyPart {
  kind: 'allocated_code'
  scopeColumns: string[]
  rawValueColumn: string
}

export type NodeKeyPart = ColumnKeyPart | AllocatedCodeKeyPart

export interface BuilderEntity {
  id: string
  termType: string
  fileId: string | null
  standardNameColumn: string
  nodeKeyParts: NodeKeyPart[]
  fieldMappings: Record<string, string>
}

export interface BuilderRelation {
  id: string
  fileId: string | null
  subjectTermType: string
  relationType: string
  objectTermType: string
}
```

- [ ] **Step 2: 创建 `csvHeader.ts`**

```typescript
// 只读文件开头一小段就够了——表头只在第一行，不需要把整个文件读进内存。
// 64KB 远超任何现实场景下单行表头的长度（哪怕几百个中文列名也远远不到这个量级）。
const HEADER_READ_BYTES = 65536

export async function readCsvHeaderColumns(file: File): Promise<string[]> {
  const chunk = await file.slice(0, HEADER_READ_BYTES).text()
  const firstLineEnd = chunk.search(/\r\n|\r|\n/)
  const firstLine = firstLineEnd === -1 ? chunk : chunk.slice(0, firstLineEnd)
  return parseCsvHeaderLine(firstLine)
}

// 按标准 CSV 引号规则（RFC 4180）解析一行，跟后端 Python csv 模块的解析规则
// 对齐——如果表头列名里本身带逗号，必须用双引号包裹（如 "A,B"），双引号
// 内部的字面双引号写成两个连续双引号（""）转义，这里同样处理这两种情况。
function parseCsvHeaderLine(line: string): string[] {
  const columns: string[] = []
  let current = ''
  let inQuotes = false
  for (let i = 0; i < line.length; i++) {
    const char = line[i]
    if (inQuotes) {
      if (char === '"') {
        if (line[i + 1] === '"') {
          current += '"'
          i++
        } else {
          inQuotes = false
        }
      } else {
        current += char
      }
    } else if (char === '"') {
      inQuotes = true
    } else if (char === ',') {
      columns.push(current)
      current = ''
    } else {
      current += char
    }
  }
  columns.push(current)
  return columns.map((c) => c.trim())
}
```

- [ ] **Step 3: 创建 `buildConfigYaml.ts`**

```typescript
import type { AddedFile, BuilderEntity, BuilderRelation } from './types'

// YAML 双引号字符串的标准转义：反斜杠、双引号、换行、制表符。所有本模块
// 生成的字符串标量一律走双引号风格，不用无引号 plain scalar——避免中文/
// 特殊字符（冒号、井号、前导连字符等）触发 YAML 语法歧义，不需要为每种
// 内容单独判断"要不要加引号"。
function yamlString(value: string): string {
  const escaped = value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\t/g, '\\t')
  return `"${escaped}"`
}

function buildEntityYamlLines(entity: BuilderEntity, filenameById: Map<string, string>): string[] {
  const lines: string[] = []
  const sourceFile = filenameById.get(entity.fileId ?? '') ?? ''
  lines.push(`  - term_type: ${yamlString(entity.termType)}`)
  lines.push(`    source_file: ${yamlString(sourceFile)}`)
  lines.push(`    standard_name_column: ${yamlString(entity.standardNameColumn)}`)
  lines.push('    node_key_parts:')
  for (const part of entity.nodeKeyParts) {
    if (part.kind === 'column') {
      lines.push(`      - column: ${yamlString(part.column)}`)
    } else {
      lines.push('      - allocated_code:')
      lines.push('          scope_columns:')
      for (const col of part.scopeColumns) {
        lines.push(`            - ${yamlString(col)}`)
      }
      lines.push(`          raw_value_column: ${yamlString(part.rawValueColumn)}`)
    }
  }
  const fieldEntries = Object.entries(entity.fieldMappings)
  if (fieldEntries.length === 0) {
    lines.push('    field_mappings: {}')
  } else {
    lines.push('    field_mappings:')
    for (const [fieldName, sourceColumn] of fieldEntries) {
      lines.push(`      ${yamlString(fieldName)}: ${yamlString(sourceColumn)}`)
    }
  }
  return lines
}

function buildRelationYamlLines(relation: BuilderRelation, filenameById: Map<string, string>): string[] {
  const sourceFile = filenameById.get(relation.fileId ?? '') ?? ''
  return [
    `  - relation_type: ${yamlString(relation.relationType)}`,
    `    source_file: ${yamlString(sourceFile)}`,
    `    subject_term_type: ${yamlString(relation.subjectTermType)}`,
    `    object_term_type: ${yamlString(relation.objectTermType)}`,
  ]
}

export function buildConfigYaml(params: {
  tenantId: string
  entities: BuilderEntity[]
  relations: BuilderRelation[]
  files: AddedFile[]
}): string {
  const filenameById = new Map(params.files.map((f) => [f.id, f.file.name]))
  const lines: string[] = [`tenant_id: ${yamlString(params.tenantId)}`, '']

  if (params.entities.length === 0) {
    lines.push('entities: []')
  } else {
    lines.push('entities:')
    for (const entity of params.entities) {
      lines.push(...buildEntityYamlLines(entity, filenameById))
    }
  }

  lines.push('')

  if (params.relations.length === 0) {
    lines.push('relations: []')
  } else {
    lines.push('relations:')
    for (const relation of params.relations) {
      lines.push(...buildRelationYamlLines(relation, filenameById))
    }
  }

  return lines.join('\n') + '\n'
}
```

- [ ] **Step 4: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 5: 手工走查（无自动化测试，靠人工核对逻辑）**

在脑内（或临时用 `node` REPL/浏览器控制台，验证后删除临时代码，不提交任何验证脚本）过一遍：给
`buildConfigYaml` 传入两个实体（一个含 `field_mappings`、一个不含）、一个关系、一个
`allocated_code` 类型的 `node_key_parts`、一个含双引号和中文的字符串值，确认输出的 YAML
文本缩进层级、双引号转义、`field_mappings: {}` 空情况都符合上面"YAML 目标格式"小节的结构。
确认 `entities`/`relations` 为空数组时输出 `entities: []`/`relations: []` 而不是
`entities:`（后面没有任何列表项的空块在 YAML 里解析结果是 `null`，不是空列表，会导致
`load_schema_etl_config` 里 `raw.get("entities") or []` 这种写法虽然凑巧兜得住，但
`data.get("entities")` 返回 `None` 时列表推导式 `[... for raw in data.get("entities") or []]`
仍然安全——这里用显式 `[]` 是更清晰的写法，不依赖这个巧合）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/types.ts frontend/src/admin/schemaEtlConfigBuilder/csvHeader.ts frontend/src/admin/schemaEtlConfigBuilder/buildConfigYaml.ts
git commit -m "feat(admin): add schema ETL config builder core logic (types, CSV header reader, YAML generator)"
```

---

### Task 2: 实体映射编辑器 `EntityMappingEditor.tsx`

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/EntityMappingEditor.tsx`

**Interfaces:**
- Consumes：Task 1 的 `BuilderEntity`、`ColumnKeyPart`、`AddedFile`、`ConfirmedTermType`（从 `./types` import——注意 `NodeKeyPart`/`AllocatedCodeKeyPart`/`ExtraFieldSpec` 虽然是 `BuilderEntity`/`ConfirmedTermType` 结构里用到的类型，但本任务代码里从不需要单独写出它们的类型标注，TypeScript 能从 `entity.nodeKeyParts`/`selectedTermType.extra_fields` 的字段类型自动推导，不要额外 import 这三个只会触发 `noUnusedLocals` 报错的类型）。
- Produces：`EntityMappingEditor` 组件，props 如下（Task 4 会渲染它）：

```typescript
interface EntityMappingEditorProps {
  entity: BuilderEntity
  files: AddedFile[]
  termTypes: ConfirmedTermType[]
  onChange: (next: BuilderEntity) => void
  onRemove: () => void
}
```

**这个组件负责编辑单条实体映射，包含四个子区域：**
1. `term_type` 下拉（选项来自 `termTypes`，只列已确认类型）——切换 `term_type` 时必须清空
   `fieldMappings`（旧类型的字段映射在新类型下没有意义，见 Step 3 的清空逻辑，这是一个
   容易漏掉的状态一致性 bug 点，务必按 Step 3 写）。
2. 文件下拉（选项来自 `files`，显示 `file.file.name`，值是 `file.id`）。
3. `standard_name_column` 下拉（选项来自"当前选中文件"的 `columns`；没选文件时禁用，显示提示文案）。
4. `node_key_parts` 编辑区：默认一个 `{kind: 'column'}` 项，"添加另一列"按钮追加更多
   `{kind: 'column'}` 项（组成复合 key）；每一项旁边有"这一列没有现成唯一编码？"的展开
   链接，点开切换该项为 `{kind: 'allocated_code'}` 形态（作用域列多选 + 原始值列单选，
   两者选项都来自当前选中文件的 `columns`）。
5. `field_mappings` 编辑区：只在选了 `term_type` 且该类型 `extra_fields.length > 0` 时
   渲染；对 `extra_fields` 里每一个字段渲染一行"该字段取哪一列（可选，不选则跳过）"的
   下拉，选项来自当前选中文件的 `columns`，加一个"不映射"空选项。

- [ ] **Step 1: 创建文件骨架和顶层结构**

```tsx
import type { AddedFile, BuilderEntity, ColumnKeyPart, ConfirmedTermType } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface EntityMappingEditorProps {
  entity: BuilderEntity
  files: AddedFile[]
  termTypes: ConfirmedTermType[]
  onChange: (next: BuilderEntity) => void
  onRemove: () => void
}

export function EntityMappingEditor({
  entity,
  files,
  termTypes,
  onChange,
  onRemove,
}: EntityMappingEditorProps) {
  const selectedFile = files.find((f) => f.id === entity.fileId) ?? null
  const columns = selectedFile?.columns ?? []
  const selectedTermType = termTypes.find((t) => t.value === entity.termType) ?? null

  return (
    <div className="flex flex-col gap-3 border-2 border-ink bg-paper p-3 shadow-brutal-sm">
      <div className="flex items-center justify-between">
        <span className="font-bold text-ink">实体映射</span>
        <button
          type="button"
          onClick={onRemove}
          className={`text-sm font-bold text-status-error underline ${focusRing}`}
        >
          删除
        </button>
      </div>
      {/* Step 2-5 依次在这里追加 */}
    </div>
  )
}
```

- [ ] **Step 2: 追加 `term_type` 下拉（切换时清空 field_mappings）**

在 `{/* Step 2-5 依次在这里追加 */}` 位置插入：

```tsx
      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        实体类型
        <select
          value={entity.termType}
          onChange={(e) => onChange({ ...entity, termType: e.target.value, fieldMappings: {} })}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {termTypes.map((t) => (
            <option key={t.value} value={t.value}>
              {t.value}
            </option>
          ))}
        </select>
      </label>
```

（`fieldMappings: {}` 就是"切换类型清空字段映射"这条规则的落地——`onChange` 每次都传
一份全新对象，`term_type` 变化的同一次更新里顺带把 `fieldMappings` 重置为空，不会有
"先改类型、字段映射还没更新"这种时序问题，因为这是同一个对象字面量里的两个字段，不是
两次分开的状态更新。）

- [ ] **Step 3: 追加文件下拉和 standard_name_column 下拉**

```tsx
      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        数据文件
        <select
          value={entity.fileId ?? ''}
          onChange={(e) => onChange({ ...entity, fileId: e.target.value || null, standardNameColumn: '' })}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {files.map((f) => (
            <option key={f.id} value={f.id}>
              {f.file.name}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        标准名称列
        <select
          value={entity.standardNameColumn}
          onChange={(e) => onChange({ ...entity, standardNameColumn: e.target.value })}
          disabled={columns.length === 0}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none disabled:opacity-50"
        >
          <option value="">{columns.length === 0 ? '请先选择数据文件' : '请选择'}</option>
          {columns.map((col) => (
            <option key={col} value={col}>
              {col}
            </option>
          ))}
        </select>
      </label>
```

（切换文件时同样把 `standardNameColumn` 重置为空——旧文件选中的列名在新文件里不一定存在。）

- [ ] **Step 4: 追加 `node_key_parts` 编辑区**

```tsx
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">Node Key（唯一标识列）</span>
        {entity.nodeKeyParts.map((part, index) => (
          <div key={index} className="flex flex-col gap-1 border border-ink/40 p-2">
            {part.kind === 'column' ? (
              <>
                <select
                  value={part.column}
                  onChange={(e) => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'column', column: e.target.value }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  disabled={columns.length === 0}
                  className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none disabled:opacity-50"
                >
                  <option value="">请选择列</option>
                  {columns.map((col) => (
                    <option key={col} value={col}>
                      {col}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'allocated_code', scopeColumns: [], rawValueColumn: '' }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  className={`self-start text-xs font-bold text-ink underline ${focusRing}`}
                >
                  这一列没有现成唯一编码？
                </button>
              </>
            ) : (
              <>
                <label className="flex flex-col gap-1 text-xs font-bold text-ink">
                  作用域列（可多选）
                  <select
                    multiple
                    value={part.scopeColumns}
                    onChange={(e) => {
                      const selected = Array.from(e.target.selectedOptions).map((o) => o.value)
                      const next = [...entity.nodeKeyParts]
                      next[index] = { ...part, scopeColumns: selected }
                      onChange({ ...entity, nodeKeyParts: next })
                    }}
                    className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    {columns.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex flex-col gap-1 text-xs font-bold text-ink">
                  原始值列
                  <select
                    value={part.rawValueColumn}
                    onChange={(e) => {
                      const next = [...entity.nodeKeyParts]
                      next[index] = { ...part, rawValueColumn: e.target.value }
                      onChange({ ...entity, nodeKeyParts: next })
                    }}
                    className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
                  >
                    <option value="">请选择列</option>
                    {columns.map((col) => (
                      <option key={col} value={col}>
                        {col}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  onClick={() => {
                    const next = [...entity.nodeKeyParts]
                    next[index] = { kind: 'column', column: '' }
                    onChange({ ...entity, nodeKeyParts: next })
                  }}
                  className={`self-start text-xs font-bold text-ink underline ${focusRing}`}
                >
                  改回简单列引用
                </button>
              </>
            )}
            {entity.nodeKeyParts.length > 1 && (
              <button
                type="button"
                onClick={() => {
                  const next = entity.nodeKeyParts.filter((_, i) => i !== index)
                  onChange({ ...entity, nodeKeyParts: next })
                }}
                className={`self-start text-xs font-bold text-status-error underline ${focusRing}`}
              >
                删除这一列
              </button>
            )}
          </div>
        ))}
        <button
          type="button"
          onClick={() => {
            const columnPart: ColumnKeyPart = { kind: 'column', column: '' }
            onChange({ ...entity, nodeKeyParts: [...entity.nodeKeyParts, columnPart] })
          }}
          className={`self-start border-2 border-ink bg-card px-3 py-1.5 text-xs font-bold text-ink shadow-brutal-sm ${focusRing}`}
        >
          + 添加另一列（组成复合 key）
        </button>
      </div>
```

- [ ] **Step 5: 追加 `field_mappings` 编辑区**

```tsx
      {selectedTermType && selectedTermType.extra_fields.length > 0 && (
        <div className="flex flex-col gap-2">
          <span className="text-sm font-bold text-ink">属性字段映射</span>
          {selectedTermType.extra_fields.map((field) => (
            <label key={field.name} className="flex flex-col gap-1 text-xs font-bold text-ink">
              {field.name}（{field.value_type}）
              <select
                value={entity.fieldMappings[field.name] ?? ''}
                onChange={(e) => {
                  const nextMappings = { ...entity.fieldMappings }
                  if (e.target.value) {
                    nextMappings[field.name] = e.target.value
                  } else {
                    delete nextMappings[field.name]
                  }
                  onChange({ ...entity, fieldMappings: nextMappings })
                }}
                className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
              >
                <option value="">不映射</option>
                {columns.map((col) => (
                  <option key={col} value={col}>
                    {col}
                  </option>
                ))}
              </select>
            </label>
          ))}
        </div>
      )}
```

- [ ] **Step 6: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 7: 手工走查**

确认：切换 `term_type` 时 `fieldMappings` 真的被清空（不是残留旧字段名指向新类型不存在
的字段）；切换文件时 `standardNameColumn` 真的被清空；`node_key_parts` 在"简单列"和
"allocated_code"两种形态之间切换时，数组其它项不受影响（用 `[...entity.nodeKeyParts]`
浅拷贝后只替换 `index` 那一项，没有整体替换导致其它项状态丢失）；`multiple` select 的
`selectedOptions` 读取方式在 TypeScript 下类型正确（`HTMLSelectElement['selectedOptions']`
是 `HTMLCollectionOf<HTMLOptionElement>`，`Array.from` 转数组后 `.value` 访问合法）。

- [ ] **Step 8: Commit**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/EntityMappingEditor.tsx
git commit -m "feat(admin): add entity mapping editor for schema ETL config builder"
```

---

### Task 3: 关系映射编辑器 `RelationMappingEditor.tsx`

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/RelationMappingEditor.tsx`

**Interfaces:**
- Consumes：Task 1 的 `BuilderRelation`、`AddedFile`、`ConfirmedCombination`（从 `./types` import）。
- Produces：`RelationMappingEditor` 组件，props：

```typescript
interface RelationMappingEditorProps {
  relation: BuilderRelation
  files: AddedFile[]
  combinations: ConfirmedCombination[]
  onChange: (next: BuilderRelation) => void
  onRemove: () => void
}
```

**这个组件负责编辑单条关系映射，只有两个字段要选：**
1. "已确认组合"下拉——选项来自 `combinations`，每项显示成 `{subject} —{relation_type}→ {object}`，
   选中后同时写入 `subjectTermType`/`relationType`/`objectTermType` 三个字段（一次 `onChange`
   调用，避免"先更新一个字段、还没更新另外两个"的中间状态）。
2. 文件下拉，跟 `EntityMappingEditor` 的文件下拉逻辑一致（选项来自 `files`）。

- [ ] **Step 1: 创建完整文件**

```tsx
import type { AddedFile, BuilderRelation, ConfirmedCombination } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface RelationMappingEditorProps {
  relation: BuilderRelation
  files: AddedFile[]
  combinations: ConfirmedCombination[]
  onChange: (next: BuilderRelation) => void
  onRemove: () => void
}

function combinationKey(c: { subject_term_type: string; relation_type: string; object_term_type: string }): string {
  return `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`
}

export function RelationMappingEditor({
  relation,
  files,
  combinations,
  onChange,
  onRemove,
}: RelationMappingEditorProps) {
  const selectedKey = combinationKey({
    subject_term_type: relation.subjectTermType,
    relation_type: relation.relationType,
    object_term_type: relation.objectTermType,
  })

  return (
    <div className="flex flex-col gap-3 border-2 border-ink bg-paper p-3 shadow-brutal-sm">
      <div className="flex items-center justify-between">
        <span className="font-bold text-ink">关系映射</span>
        <button
          type="button"
          onClick={onRemove}
          className={`text-sm font-bold text-status-error underline ${focusRing}`}
        >
          删除
        </button>
      </div>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        关系组合
        <select
          value={relation.subjectTermType ? selectedKey : ''}
          onChange={(e) => {
            if (!e.target.value) {
              onChange({ ...relation, subjectTermType: '', relationType: '', objectTermType: '' })
              return
            }
            const [subject, rel, object] = e.target.value.split('|')
            onChange({ ...relation, subjectTermType: subject, relationType: rel, objectTermType: object })
          }}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {combinations.map((c) => (
            <option key={combinationKey(c)} value={combinationKey(c)}>
              {c.subject_term_type} —{c.relation_type}→ {c.object_term_type}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1 text-sm font-bold text-ink">
        数据文件
        <select
          value={relation.fileId ?? ''}
          onChange={(e) => onChange({ ...relation, fileId: e.target.value || null })}
          className="border-2 border-ink bg-card px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
        >
          <option value="">请选择</option>
          {files.map((f) => (
            <option key={f.id} value={f.id}>
              {f.file.name}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
```

（`combinationKey` 用 `|` 分隔三段拼成一个可以安全 `split` 回三个字段的字符串——
`relation_type` 已经是 `^[A-Z][A-Z0-9_]{0,63}$` 格式，`term_type` 目前没有排除 `|`
字符的校验，理论上如果某个 term_type 名字本身含 `|` 会导致 `split('|')` 解析出错位的
三段；这是一个已知的极窄边界情况，值得在自查里过一遍，如果发现真实本体里 term_type
命名允许 `|`，后续可以把分隔符换成一个更不可能出现在业务命名里的字符，比如 ` `
或直接从 `combinations` 数组按下标反查而不是编码进字符串——但这属于极端边界，本任务先
用 `|` 分隔符实现，不要在这里过度设计。）

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 3: 手工走查**

确认选中一个组合后 `subjectTermType`/`relationType`/`objectTermType` 三个字段确实
同时被设置正确（用 `console.log` 或断点在浏览器里手动验证一次，验证完删除调试代码）；
确认清空选择（`value=""`）时三个字段都被清空，不会残留旧值导致后续 YAML 生成出脏数据。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/RelationMappingEditor.tsx
git commit -m "feat(admin): add relation mapping editor for schema ETL config builder"
```

---

### Task 4: 编排组件 + 接入 `SchemaEtlPage.tsx`

**Files:**
- Create: `frontend/src/admin/schemaEtlConfigBuilder/SchemaEtlConfigBuilder.tsx`
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`

**Interfaces:**
- Consumes：Task 1 的 `types.ts`/`csvHeader.ts`/`buildConfigYaml.ts`，Task 2 的
  `EntityMappingEditor`，Task 3 的 `RelationMappingEditor`。`SchemaEtlPage.tsx` 已有的
  `adminFetch`/`extractErrorDetail`（从 `./adminApi`）、`useAdminAuth`/`useAdminTenant`。
- Produces：`SchemaEtlConfigBuilder` 组件，props：

```typescript
interface SchemaEtlConfigBuilderProps {
  tenantId: string
  sessionToken: string
  disabled: boolean       // 对齐现有裸上传表单的 confirmed !== true 禁用逻辑
  onSubmitted: () => void // 提交成功后通知父组件刷新跑批列表（复用 pollNowRef 机制）
}
```

- [ ] **Step 1: 创建组件骨架 + 本体数据拉取**

```tsx
import { useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { buildConfigYaml } from './buildConfigYaml'
import { readCsvHeaderColumns } from './csvHeader'
import { EntityMappingEditor } from './EntityMappingEditor'
import { RelationMappingEditor } from './RelationMappingEditor'
import type { AddedFile, BuilderEntity, BuilderRelation, ConfirmedCombination, ConfirmedTermType } from './types'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface SchemaEtlConfigBuilderProps {
  tenantId: string
  sessionToken: string
  disabled: boolean
  onSubmitted: () => void
}

export function SchemaEtlConfigBuilder({
  tenantId,
  sessionToken,
  disabled,
  onSubmitted,
}: SchemaEtlConfigBuilderProps) {
  const [termTypes, setTermTypes] = useState<ConfirmedTermType[]>([])
  const [combinations, setCombinations] = useState<ConfirmedCombination[]>([])
  const [files, setFiles] = useState<AddedFile[]>([])
  const [entities, setEntities] = useState<BuilderEntity[]>([])
  const [relations, setRelations] = useState<BuilderRelation[]>([])
  const [fileError, setFileError] = useState<string | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const [termTypesRes, combinationsRes] = await Promise.all([
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=confirmed`, sessionToken),
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=confirmed`, sessionToken),
      ])
      if (cancelled) return
      const termTypesData = (await termTypesRes.json()) as { term_types: ConfirmedTermType[] }
      const combinationsData = (await combinationsRes.json()) as { constraints: ConfirmedCombination[] }
      if (cancelled) return
      setTermTypes(termTypesData.term_types)
      setCombinations(combinationsData.constraints)
    }
    load().catch((err) => console.error('加载本体数据失败', err))
    return () => {
      cancelled = true
    }
  }, [tenantId, sessionToken])

  // Step 2-6 依次在这里追加
  return null
}
```

- [ ] **Step 2: 追加"添加文件"逻辑**

在 `// Step 2-6 依次在这里追加` 上方插入：

```tsx
  const handleAddFiles = async (fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return
    setFileError(null)
    try {
      const newFiles: AddedFile[] = []
      for (const file of Array.from(fileList)) {
        const columns = await readCsvHeaderColumns(file)
        newFiles.push({ id: crypto.randomUUID(), file, columns })
      }
      setFiles((prev) => [...prev, ...newFiles])
    } catch (err) {
      setFileError(err instanceof Error ? err.message : '读取文件表头失败')
    }
  }

  const handleAddEntity = () => {
    setEntities((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        termType: '',
        fileId: null,
        standardNameColumn: '',
        nodeKeyParts: [{ kind: 'column', column: '' }],
        fieldMappings: {},
      },
    ])
  }

  const handleAddRelation = () => {
    setRelations((prev) => [
      ...prev,
      { id: crypto.randomUUID(), fileId: null, subjectTermType: '', relationType: '', objectTermType: '' },
    ])
  }
```

- [ ] **Step 3: 追加"添加文件"按钮 + 已添加文件列表的 JSX**

把函数体的 `return null` 替换成（先只放这一部分，后续 Step 继续在同一个 JSX 树里追加）：

```tsx
  return (
    <div className="flex flex-col gap-4 border-t-2 border-ink p-4">
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">1. 添加数据文件</span>
        <input
          type="file"
          accept=".csv"
          multiple
          disabled={disabled}
          onChange={(e) => {
            handleAddFiles(e.target.files).catch((err) => console.error(err))
            e.target.value = ''
          }}
          className="text-ink"
        />
        {fileError && (
          <p role="alert" className="text-sm text-ink">
            {fileError}
          </p>
        )}
        {files.length > 0 && (
          <ul className="flex flex-col gap-1 text-sm text-ink">
            {files.map((f) => (
              <li key={f.id}>
                {f.file.name}（{f.columns.length} 列）
              </li>
            ))}
          </ul>
        )}
      </div>
      {/* Step 4-6 依次在这里追加 */}
    </div>
  )
```

- [ ] **Step 4: 追加实体/关系映射列表区域**

在 `{/* Step 4-6 依次在这里追加 */}` 位置插入：

```tsx
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">2. 配置实体映射</span>
        {entities.map((entity) => (
          <EntityMappingEditor
            key={entity.id}
            entity={entity}
            files={files}
            termTypes={termTypes}
            onChange={(next) => setEntities((prev) => prev.map((e) => (e.id === next.id ? next : e)))}
            onRemove={() => setEntities((prev) => prev.filter((e) => e.id !== entity.id))}
          />
        ))}
        <button
          type="button"
          onClick={handleAddEntity}
          disabled={disabled || files.length === 0}
          className={`self-start border-2 border-ink bg-card px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          + 添加实体映射
        </button>
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">3. 配置关系映射（可选）</span>
        {relations.map((relation) => (
          <RelationMappingEditor
            key={relation.id}
            relation={relation}
            files={files}
            combinations={combinations}
            onChange={(next) => setRelations((prev) => prev.map((r) => (r.id === next.id ? next : r)))}
            onRemove={() => setRelations((prev) => prev.filter((r) => r.id !== relation.id))}
          />
        ))}
        <button
          type="button"
          onClick={handleAddRelation}
          disabled={disabled || files.length === 0}
          className={`self-start border-2 border-ink bg-card px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          + 添加关系映射
        </button>
      </div>
```

- [ ] **Step 5: 追加 YAML 预览 + 提交**

在 Step 4 插入内容之后、`</div>` 收尾标签之前插入：

```tsx
      <div className="flex flex-col gap-2">
        <span className="text-sm font-bold text-ink">4. 预览并提交</span>
        <pre className="max-h-80 overflow-auto border-2 border-ink bg-card p-3 text-xs text-ink">
          {buildConfigYaml({ tenantId, entities, relations, files })}
        </pre>
        {submitError && (
          <p role="alert" className="text-sm text-ink">
            {submitError}
          </p>
        )}
        <button
          type="button"
          disabled={disabled || submitting || entities.length === 0}
          onClick={async () => {
            setSubmitError(null)
            setSubmitting(true)
            try {
              const yamlText = buildConfigYaml({ tenantId, entities, relations, files })
              const formData = new FormData()
              formData.append('config', new Blob([yamlText], { type: 'text/yaml' }), 'config.yaml')
              for (const f of files) {
                formData.append('data_files', f.file)
              }
              const response = await adminFetch(
                `/api/admin/${encodeURIComponent(tenantId)}/schema-etl/runs`,
                sessionToken,
                { method: 'POST', body: formData },
              )
              if (!response.ok) {
                const body = await response.json().catch(() => ({}))
                throw new Error(extractErrorDetail(body, '启动失败'))
              }
              setFiles([])
              setEntities([])
              setRelations([])
              onSubmitted()
            } catch (err) {
              setSubmitError(err instanceof Error ? err.message : '启动失败')
            } finally {
              setSubmitting(false)
            }
          }}
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {submitting ? '提交中…' : '确认并开始运行'}
        </button>
      </div>
```

（提交成功后清空 `files`/`entities`/`relations`，回到初始状态，跟现有裸上传表单
`handleUpload` 里 `form.reset()` 的收尾语义一致——避免用户重新打开这个区块时还看到
上一次已经提交过的内容。）

- [ ] **Step 6: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 7: 接入 `SchemaEtlPage.tsx`**

在 `frontend/src/admin/SchemaEtlPage.tsx` 顶部 import 区（`import { adminFetch, extractErrorDetail } from './adminApi'` 那一行附近）新增：

```tsx
import { SchemaEtlConfigBuilder } from './schemaEtlConfigBuilder/SchemaEtlConfigBuilder'
```

找到现有代码（"查看示例数据"折叠区块收尾、裸上传表单开始之间）：

```tsx
        )}
      </div>

      <form
        onSubmit={handleUpload}
```

在这两者之间插入一个同样默认折叠的新区块（复用"查看示例数据"那个折叠按钮的交互模式，
新增一个独立的 `builderExpanded` state）：

```tsx
        )}
      </div>

      <div className="flex flex-col gap-2 border-2 border-ink bg-card shadow-brutal-sm">
        <button
          type="button"
          onClick={() => setBuilderExpanded((prev) => !prev)}
          className={`flex items-center justify-between px-4 py-3 text-left font-bold text-ink ${focusRing}`}
        >
          <span>
            配置构建向导
            <span className="ml-2 font-normal text-ink-soft">
              对着自己的数据列一步步配出 config.yaml，不用手写 YAML
            </span>
          </span>
          <span aria-hidden="true">{builderExpanded ? '▾' : '▸'}</span>
        </button>
        {builderExpanded && sessionToken && (
          <SchemaEtlConfigBuilder
            tenantId={tenantId}
            sessionToken={sessionToken}
            disabled={confirmed !== true}
            onSubmitted={() => {
              pollNowRef.current()
            }}
          />
        )}
      </div>

      <form
        onSubmit={handleUpload}
```

在函数体内 `const [downloadingSample, setDownloadingSample] = useState(false)` 那一行
（"查看示例数据"区块用到的最后一个 state）之后新增一行：

```tsx
  const [builderExpanded, setBuilderExpanded] = useState(false)
```

- [ ] **Step 8: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）

- [ ] **Step 9: 手工走查**

逐条确认：本体数据拉取 `useEffect` 的依赖数组是 `[tenantId, sessionToken]`，不包含任何
组件自己 `setState` 写入的值——不会重演上一个 SDD 任务里"依赖数组含自己写的 state 导致
自我取消"的那个 bug（对照检查：这里没有 `loading`/`data` 类的 state 出现在依赖数组里，
`cancelled` 标志只用来在组件卸载后丢弃过期结果，不参与依赖数组，逻辑上是安全的）。确认
"添加文件"按钮选中同一个文件两次会往 `files` 里追加两条独立记录（每条有独立 `id`），
不会因为 `File` 对象引用不同（浏览器每次选择都会产生新的 `File` 实例）而互相覆盖。确认
提交按钮在 `entities.length === 0` 时禁用，不会提交一份没有任何实体映射的空配置（避免
触发后端 `SchemaETLNotConfirmedError` 之外的另一种"配置合法但内容空洞"的困惑）。

- [ ] **Step 10: Commit**

```bash
git add frontend/src/admin/schemaEtlConfigBuilder/SchemaEtlConfigBuilder.tsx frontend/src/admin/SchemaEtlPage.tsx
git commit -m "feat(admin): add schema ETL config builder wizard, wire into SchemaEtlPage"
```

---

## 完成后的整体验证

1. `cd frontend && npx tsc --noEmit` 干净。
2. 手动核对（无浏览器自动化工具，口头确认设计意图）：新区块默认折叠，展开后先"添加
   文件"、再"添加实体/关系映射"、最后"预览并提交"这个顺序流程符合直觉；两条实体/关系
   映射选中同一个文件时表头列表一致（因为都从同一个 `AddedFile.columns` 读取，不会出现
   不一致）；提交时 `FormData` 的字段名/结构跟现有裸上传表单逐字段一致，后端不需要区分
   两条提交路径。
