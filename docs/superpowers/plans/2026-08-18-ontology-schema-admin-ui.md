# 本体 Schema 管理后台界面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给已有的本体 schema 后端接口（`app/api/admin_ontology_routes.py`，实体类型/关系类型/约束/产品线的 CRUD + draft/confirm 生命周期）加一个管理后台界面——目前这些接口只能直接调 API，没有任何前端页面。

**Architecture:** 纯前端改动，不碰后端（接口已完整存在）。新增一个页面 `OntologySchemaPage.tsx`，内部用4个 tab 切换实体类型/关系类型/约束/产品线四类资源，复用已有的 `adminFetch`/`extractErrorDetail`/`useAdminAuth`/`useAdminTenant` 基础设施，交互模式与 `TermsPage.tsx`（行内表格增删改）、`SchemaEtlPage.tsx`（tab 内多个区块）保持一致。

**Tech Stack:** React + TypeScript，无新依赖。

**Spec:** `docs/superpowers/specs/2026-08-18-ontology-schema-admin-ui-design.md`

## Global Constraints

- 只加前端文件，不修改任何后端 Python 文件——`docs/superpowers/specs/2026-08-18-ontology-schema-admin-ui-design.md` 第1节已确认所有需要的接口都已存在。
- 约束 tab 的 `relation_type` 下拉框数据源必须是**该租户草稿状态**的关系类型列表（`?status=draft`），不是已确认的——这是后端 `_validate_references` 的既有校验口径，选错数据源会导致"刚建的关系类型选了却报未知"这种假故障（spec 第1节、第8节）。
- `checkout_draft` 对用户透明——进入"关系类型"或"约束" tab 时自动调用一次，不需要用户点按钮（spec 第3节）。
- `node_key_template` 是纯文本输入框，不做任何格式校验（spec 第1节：这个字段目前没有代码消费它）。
- 关系类型的"迁移图谱边"（`POST .../relation-types/migrate`）是独立于改名（`PUT`）的次要操作，必须有自己的二次确认文案，不能和改名共用一个按钮（spec 第7节）。
- "确认" schema 操作前只做简单 `window.confirm` 二次确认，不做 diff 预览（spec 第5节）。
- 产品线 tab 是全局的、不受当前租户切换影响，tab 内必须有提示文案说明这一点（spec 第2节、第9节）。

---

### Task 1: `OntologySchemaPage.tsx` 页面

**Files:**
- Create: `frontend/src/admin/OntologySchemaPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`

**Interfaces:**
- Consumes: `frontend/src/admin/adminApi.ts` 的 `adminFetch`/`extractErrorDetail`；`frontend/src/admin/useAdminAuth.ts` 的 `useAdminAuth`；`frontend/src/admin/TenantContext.tsx` 的 `useAdminTenant`（三者都已存在，直接复用，不新建）；`app/api/admin_ontology_routes.py` 的全部既有接口（不改动，见下方"接口清单"）。
- Produces: `OntologySchemaPage` 组件，挂载到 `/admin/ontology` 路由，导航栏新增入口。

**接口清单**（本任务只消费，不修改任何一个）：

| 方法+路径 | 请求体 | 响应体 |
|---|---|---|
| `GET /api/admin/ontology/{tenant_id}/status` | — | `{"confirmed": bool}` |
| `GET /api/admin/ontology/{tenant_id}/term-types` | — | `{"term_types": [{"value": str, "extra_fields": [{"name": str, "value_type": str}], "node_key_template": str}]}` |
| `POST /api/admin/ontology/{tenant_id}/term-types` | `{"value": str, "extra_fields": [...], "node_key_template": str}` | 同上单条 |
| `PUT /api/admin/ontology/{tenant_id}/term-types/{value}` | 同 POST | 同上单条 |
| `DELETE /api/admin/ontology/{tenant_id}/term-types/{value}` | — | `{"deleted": true}`，409 若被引用 |
| `GET /api/admin/ontology/product-lines` | — | `{"product_lines": [str, ...]}` |
| `POST /api/admin/ontology/product-lines` | `{"value": str}` | `{"value": str}` |
| `PUT /api/admin/ontology/product-lines/{value}` | `{"value": str}`（新名字） | `{"value": str}` |
| `DELETE /api/admin/ontology/product-lines/{value}` | — | `{"deleted": true}`，409 若被引用 |
| `GET /api/admin/ontology/{tenant_id}/relation-types?status=draft\|confirmed` | — | `{"relation_types": [{"relation_type": str, "example_phrase": str, "description": str, "allow_chain_query": bool, "source": str}]}` |
| `POST /api/admin/ontology/{tenant_id}/relation-types` | `{"relation_type": str, "example_phrase": str, "description": str, "allow_chain_query": bool}` | 同上单条 |
| `PUT /api/admin/ontology/{tenant_id}/relation-types/{relation_type}` | 同 POST（`relation_type` 是新名字） | 同上单条 |
| `DELETE /api/admin/ontology/{tenant_id}/relation-types/{relation_type}` | — | `{"deleted": true}` |
| `POST /api/admin/ontology/{tenant_id}/relation-types/migrate` | `{"old_type": str, "new_type": str}` | `{"migrated_count": int}` |
| `GET /api/admin/ontology/{tenant_id}/constraints?status=draft\|confirmed` | — | `{"constraints": [{"subject_term_type": str, "relation_type": str, "object_term_type": str}]}` |
| `POST /api/admin/ontology/{tenant_id}/constraints` | `{"subject_term_type": str, "relation_type": str, "object_term_type": str}` | 同上单条 |
| `DELETE /api/admin/ontology/{tenant_id}/constraints` | 同 POST（DELETE 带请求体） | `{"deleted": true}` |
| `POST /api/admin/ontology/{tenant_id}/checkout` | — | `{"checked_out": true}` |
| `POST /api/admin/ontology/{tenant_id}/confirm` | — | `{"confirmed": true}` |

所有接口都要求 `Authorization: Bearer <session_token>`（`adminFetch` 自动带上），全部走 `deps.require_admin_session`。

- [ ] **Step 1: 实现**

（前端没有已建立的单元测试基础设施覆盖管理页面组件——`DocumentsPage.tsx`/`TermsPage.tsx`/`SchemaEtlPage.tsx` 都没有对应的 `.test.tsx` 文件，本任务跟随这个既有惯例，用手动走查代替，见 Step 2。）

创建 `frontend/src/admin/OntologySchemaPage.tsx`：

```tsx
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

type Tab = 'term-types' | 'relation-types' | 'constraints' | 'product-lines'
type ViewMode = 'draft' | 'confirmed'

interface ExtraFieldSpec {
  name: string
  value_type: string
}

interface TermType {
  value: string
  extra_fields: ExtraFieldSpec[]
  node_key_template: string
}

interface RelationType {
  relation_type: string
  example_phrase: string
  description: string
  allow_chain_query: boolean
  source: string
}

interface Constraint {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

const VALUE_TYPES = ['string', 'number', 'integer', 'number[]'] as const

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const tabButtonClass = (active: boolean) =>
  `border-2 border-ink px-3 py-2 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

const emptyTermTypeDraft = (): TermType => ({ value: '', extra_fields: [], node_key_template: '' })
const emptyRelationTypeDraft = (): RelationType => ({
  relation_type: '',
  example_phrase: '',
  description: '',
  allow_chain_query: false,
  source: '',
})

export function OntologySchemaPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [tab, setTab] = useState<Tab>('term-types')
  const [confirmed, setConfirmed] = useState<boolean | null>(null)
  const [pageError, setPageError] = useState<string | null>(null)

  useEffect(() => {
    document.title = '本体 Schema 管理 · 管理后台'
  }, [])

  const refreshStatus = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/status`,
      sessionToken,
    )
    const data = (await response.json()) as { confirmed: boolean }
    setConfirmed(data.confirmed)
  }, [sessionToken, tenantId])

  useEffect(() => {
    refreshStatus().catch((err) => console.error('查询 schema 确认状态失败', err))
  }, [refreshStatus])

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-ink">本体 Schema 管理（租户：{tenantId}）</h1>
        <span
          className={`border-2 border-ink px-3 py-1.5 text-sm font-bold shadow-brutal-sm ${
            confirmed ? 'bg-accent-green text-ink' : 'bg-accent-yellow text-ink'
          }`}
        >
          {confirmed === null ? '加载中…' : confirmed ? '已确认' : '草稿中（未确认）'}
        </span>
      </div>

      {pageError && (
        <p role="alert" className="border-2 border-status-error bg-card px-3 py-2 text-sm text-ink shadow-brutal-sm">
          {pageError}
        </p>
      )}

      <nav className="flex flex-row flex-wrap gap-2">
        <button type="button" className={tabButtonClass(tab === 'term-types')} onClick={() => setTab('term-types')}>
          实体类型
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'relation-types')}
          onClick={() => setTab('relation-types')}
        >
          关系类型
        </button>
        <button type="button" className={tabButtonClass(tab === 'constraints')} onClick={() => setTab('constraints')}>
          约束
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'product-lines')}
          onClick={() => setTab('product-lines')}
        >
          产品线
        </button>
      </nav>

      {tab === 'term-types' && (
        <TermTypesTab
          sessionToken={sessionToken}
          tenantId={tenantId}
          onError={setPageError}
        />
      )}
      {tab === 'relation-types' && (
        <RelationTypesTab
          sessionToken={sessionToken}
          tenantId={tenantId}
          onError={setPageError}
          onConfirmed={refreshStatus}
        />
      )}
      {tab === 'constraints' && (
        <ConstraintsTab sessionToken={sessionToken} tenantId={tenantId} onError={setPageError} />
      )}
      {tab === 'product-lines' && (
        <ProductLinesTab sessionToken={sessionToken} onError={setPageError} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 实体类型 tab
// ---------------------------------------------------------------------------

function TermTypesTab({
  sessionToken,
  tenantId,
  onError,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
}) {
  const [items, setItems] = useState<TermType[]>([])
  const [loaded, setLoaded] = useState(false)
  const [editingValue, setEditingValue] = useState<string | null>(null)
  const [draft, setDraft] = useState<TermType>(emptyTermTypeDraft())
  const [creating, setCreating] = useState(false)
  const [savingValue, setSavingValue] = useState<string | null>(null)
  const [deletingValue, setDeletingValue] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`,
      sessionToken,
    )
    const data = (await response.json()) as { term_types: TermType[] }
    setItems(data.term_types)
    setLoaded(true)
  }, [sessionToken, tenantId])

  useEffect(() => {
    refresh().catch((err) => console.error('实体类型列表刷新失败', err))
  }, [refresh])

  const startEdit = (item: TermType) => {
    setEditingValue(item.value)
    setDraft({ ...item, extra_fields: item.extra_fields.map((f) => ({ ...f })) })
  }

  const startCreate = () => {
    setEditingValue('')
    setDraft(emptyTermTypeDraft())
  }

  const cancelEdit = () => {
    setEditingValue(null)
    setDraft(emptyTermTypeDraft())
  }

  const addField = () => {
    setDraft((prev) => ({ ...prev, extra_fields: [...prev.extra_fields, { name: '', value_type: 'string' }] }))
  }

  const updateField = (index: number, patch: Partial<ExtraFieldSpec>) => {
    setDraft((prev) => ({
      ...prev,
      extra_fields: prev.extra_fields.map((f, i) => (i === index ? { ...f, ...patch } : f)),
    }))
  }

  const removeField = (index: number) => {
    setDraft((prev) => ({ ...prev, extra_fields: prev.extra_fields.filter((_, i) => i !== index) }))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || editingValue === null) return
    const isCreate = editingValue === ''
    onError(null)
    if (isCreate) setCreating(true)
    else setSavingValue(editingValue)
    try {
      const url = isCreate
        ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`
        : `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(editingValue)}`
      const response = await adminFetch(url, sessionToken, {
        method: isCreate ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, isCreate ? '新增实体类型失败' : '更新实体类型失败'))
      }
      cancelEdit()
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setCreating(false)
      setSavingValue(null)
    }
  }

  const handleDelete = async (value: string) => {
    if (!sessionToken || deletingValue !== null) return
    if (!window.confirm(`确定要删除实体类型「${value}」吗？此操作不可撤销。`)) return
    onError(null)
    setDeletingValue(value)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(value)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除实体类型失败'))
      }
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeletingValue(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {loaded && items.length === 0 && editingValue === null && (
        <p className="text-ink-soft">还没有定义任何实体类型。</p>
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className="px-3 py-2">类型名</th>
                <th className="px-3 py-2">node_key_template</th>
                <th className="px-3 py-2">属性字段数</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.value} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className="px-3 py-2">{item.value}</td>
                  <td className="px-3 py-2 font-mono text-xs">{item.node_key_template || '-'}</td>
                  <td className="px-3 py-2">{item.extra_fields.length}</td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      className={`mr-2 font-bold underline ${focusRing}`}
                      onClick={() => startEdit(item)}
                      disabled={editingValue !== null}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                      onClick={() => handleDelete(item.value)}
                      disabled={deletingValue !== null}
                    >
                      {deletingValue === item.value ? '删除中…' : '删除'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editingValue === null && (
        <button
          type="button"
          onClick={startCreate}
          className={`self-start border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm ${focusRing}`}
        >
          + 新增实体类型
        </button>
      )}

      {editingValue !== null && (
        <form onSubmit={submit} className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            类型名
            <input
              type="text"
              required
              value={draft.value}
              onChange={(e) => setDraft((prev) => ({ ...prev, value: e.target.value }))}
              className="border-2 border-ink px-2 py-1.5"
            />
          </label>
          {editingValue !== '' && (
            <p className="text-xs text-ink-soft">改名会立即级联更新所有引用该类型的术语记录，没有草稿缓冲。</p>
          )}
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            node_key_template
            <input
              type="text"
              value={draft.node_key_template}
              onChange={(e) => setDraft((prev) => ({ ...prev, node_key_template: e.target.value }))}
              className="border-2 border-ink px-2 py-1.5 font-mono"
            />
          </label>

          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-ink">属性字段</span>
            {draft.extra_fields.map((field, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  placeholder="字段名"
                  required
                  value={field.name}
                  onChange={(e) => updateField(index, { name: e.target.value })}
                  className="border-2 border-ink px-2 py-1.5"
                />
                <select
                  value={field.value_type}
                  onChange={(e) => updateField(index, { value_type: e.target.value })}
                  className="border-2 border-ink px-2 py-1.5"
                >
                  {VALUE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => removeField(index)}
                  className={`font-bold text-status-error underline ${focusRing}`}
                >
                  删除
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={addField}
              className={`self-start border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink ${focusRing}`}
            >
              + 添加字段
            </button>
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || savingValue !== null}
              className={`border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingValue !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 关系类型 tab
// ---------------------------------------------------------------------------

function RelationTypesTab({
  sessionToken,
  tenantId,
  onError,
  onConfirmed,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  onConfirmed: () => Promise<void>
}) {
  const [view, setView] = useState<ViewMode>('draft')
  const [items, setItems] = useState<RelationType[]>([])
  const [loaded, setLoaded] = useState(false)
  const [editingType, setEditingType] = useState<string | null>(null)
  const [draft, setDraft] = useState<RelationType>(emptyRelationTypeDraft())
  const [creating, setCreating] = useState(false)
  const [savingType, setSavingType] = useState<string | null>(null)
  const [deletingType, setDeletingType] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [migratingFrom, setMigratingFrom] = useState<string | null>(null)
  const [migrateTarget, setMigrateTarget] = useState('')
  const [migrating, setMigrating] = useState(false)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    // checkout 对用户透明——每次进这个 tab / 切换视图前，先确保草稿存在，
    // 幂等操作，已有草稿时后端直接跳过（见 app/graphrag/ontology_lifecycle.py
    // ::checkout_draft 的说明）。
    await adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`, sessionToken, {
      method: 'POST',
    })
    const response = await adminFetch(
      `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=${view}`,
      sessionToken,
    )
    const data = (await response.json()) as { relation_types: RelationType[] }
    setItems(data.relation_types)
    setLoaded(true)
  }, [sessionToken, tenantId, view])

  useEffect(() => {
    refresh().catch((err) => console.error('关系类型列表刷新失败', err))
  }, [refresh])

  const startEdit = (item: RelationType) => {
    setEditingType(item.relation_type)
    setDraft({ ...item })
  }

  const startCreate = () => {
    setEditingType('')
    setDraft(emptyRelationTypeDraft())
  }

  const cancelEdit = () => {
    setEditingType(null)
    setDraft(emptyRelationTypeDraft())
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || editingType === null) return
    const isCreate = editingType === ''
    onError(null)
    if (isCreate) setCreating(true)
    else setSavingType(editingType)
    try {
      const url = isCreate
        ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types`
        : `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/${encodeURIComponent(editingType)}`
      const response = await adminFetch(url, sessionToken, {
        method: isCreate ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          relation_type: draft.relation_type,
          example_phrase: draft.example_phrase,
          description: draft.description,
          allow_chain_query: draft.allow_chain_query,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, isCreate ? '新增关系类型失败' : '更新关系类型失败'))
      }
      cancelEdit()
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setCreating(false)
      setSavingType(null)
    }
  }

  const handleDelete = async (relationType: string) => {
    if (!sessionToken || deletingType !== null) return
    if (!window.confirm(`确定要删除关系类型「${relationType}」吗？此操作不可撤销。`)) return
    onError(null)
    setDeletingType(relationType)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/${encodeURIComponent(relationType)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除关系类型失败'))
      }
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setDeletingType(null)
    }
  }

  const handleConfirm = async () => {
    if (!sessionToken || confirming) return
    if (
      !window.confirm(
        `确认后，当前草稿将成为新的已确认版本，旧的已确认版本会被换掉、无法恢复。确认要确认租户「${tenantId}」吗？`,
      )
    ) {
      return
    }
    onError(null)
    setConfirming(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/confirm`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '确认失败'))
      }
      await onConfirmed()
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '确认失败')
    } finally {
      setConfirming(false)
    }
  }

  const handleMigrate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || migratingFrom === null || migrating) return
    if (
      !window.confirm(
        `这会遍历租户「${tenantId}」在 Neo4j 图谱里所有类型为「${migratingFrom}」的边，批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      )
    ) {
      return
    }
    onError(null)
    setMigrating(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/migrate`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_type: migratingFrom, new_type: migrateTarget }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '迁移图谱边失败'))
      }
      const data = (await response.json()) as { migrated_count: number }
      window.alert(`已迁移 ${data.migrated_count} 条边`)
      setMigratingFrom(null)
      setMigrateTarget('')
    } catch (err) {
      onError(err instanceof Error ? err.message : '迁移图谱边失败')
    } finally {
      setMigrating(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-sm font-bold text-ink">
          <input
            type="checkbox"
            checked={view === 'confirmed'}
            onChange={(e) => setView(e.target.checked ? 'confirmed' : 'draft')}
          />
          查看已确认版本（只读）
        </label>
        {view === 'draft' && (
          <button
            type="button"
            onClick={handleConfirm}
            disabled={confirming}
            className={`border-2 border-ink bg-accent-green px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
          >
            {confirming ? '确认中…' : '确认 schema'}
          </button>
        )}
      </div>

      {loaded && items.length === 0 && <p className="text-ink-soft">还没有任何{view === 'draft' ? '草稿' : '已确认的'}关系类型。</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className="px-3 py-2">关系类型</th>
                <th className="px-3 py-2">示例短语</th>
                <th className="px-3 py-2">说明</th>
                <th className="px-3 py-2">支持链式查询</th>
                {view === 'draft' && <th className="px-3 py-2">操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.relation_type} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className="px-3 py-2 font-mono text-xs">{item.relation_type}</td>
                  <td className="px-3 py-2">{item.example_phrase}</td>
                  <td className="px-3 py-2">{item.description || '-'}</td>
                  <td className="px-3 py-2">{item.allow_chain_query ? '是' : '否'}</td>
                  {view === 'draft' && (
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        className={`mr-2 font-bold underline ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={editingType !== null}
                      >
                        改名/编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline ${focusRing}`}
                        onClick={() => {
                          setMigratingFrom(item.relation_type)
                          setMigrateTarget('')
                        }}
                      >
                        迁移图谱边…
                      </button>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleDelete(item.relation_type)}
                        disabled={deletingType !== null}
                      >
                        {deletingType === item.relation_type ? '删除中…' : '删除'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'draft' && (
        <p className="text-xs text-ink-soft">
          改名只影响草稿定义，已确认图谱里的历史边不会自动变，需要用「迁移图谱边」处理。
        </p>
      )}

      {view === 'draft' && editingType === null && (
        <button
          type="button"
          onClick={startCreate}
          className={`self-start border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm ${focusRing}`}
        >
          + 新增关系类型
        </button>
      )}

      {view === 'draft' && editingType !== null && (
        <form onSubmit={submit} className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            关系类型名（大写字母/数字/下划线）
            <input
              type="text"
              required
              value={draft.relation_type}
              onChange={(e) => setDraft((prev) => ({ ...prev, relation_type: e.target.value }))}
              className="border-2 border-ink px-2 py-1.5 font-mono"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            示例短语
            <input
              type="text"
              required
              value={draft.example_phrase}
              onChange={(e) => setDraft((prev) => ({ ...prev, example_phrase: e.target.value }))}
              className="border-2 border-ink px-2 py-1.5"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            说明
            <input
              type="text"
              value={draft.description}
              onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))}
              className="border-2 border-ink px-2 py-1.5"
            />
          </label>
          <label className="flex items-center gap-2 text-sm font-bold text-ink">
            <input
              type="checkbox"
              checked={draft.allow_chain_query}
              onChange={(e) => setDraft((prev) => ({ ...prev, allow_chain_query: e.target.checked }))}
            />
            支持链式查询
          </label>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || savingType !== null}
              className={`border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingType !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {migratingFrom !== null && (
        <form
          onSubmit={handleMigrate}
          className="flex flex-col gap-3 border-2 border-status-error bg-card p-4 shadow-brutal"
        >
          <p className="text-sm text-ink">
            把租户「{tenantId}」图谱里所有类型为「{migratingFrom}」的边迁移成：
          </p>
          <input
            type="text"
            required
            value={migrateTarget}
            onChange={(e) => setMigrateTarget(e.target.value)}
            placeholder="新类型名"
            className="border-2 border-ink px-2 py-1.5 font-mono"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={migrating}
              className={`border-2 border-ink bg-status-error px-4 py-2 text-sm font-bold text-white shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
            >
              {migrating ? '迁移中…' : '确认迁移'}
            </button>
            <button
              type="button"
              onClick={() => setMigratingFrom(null)}
              className={`border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 约束 tab
// ---------------------------------------------------------------------------

function ConstraintsTab({
  sessionToken,
  tenantId,
  onError,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
}) {
  const [view, setView] = useState<ViewMode>('draft')
  const [constraints, setConstraints] = useState<Constraint[]>([])
  const [termTypes, setTermTypes] = useState<string[]>([])
  const [draftRelationTypes, setDraftRelationTypes] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  const [subject, setSubject] = useState('')
  const [relationType, setRelationType] = useState('')
  const [object, setObject] = useState('')
  const [adding, setAdding] = useState(false)
  const [removingKey, setRemovingKey] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    await adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`, sessionToken, {
      method: 'POST',
    })
    const [constraintsRes, termTypesRes, relationTypesRes] = await Promise.all([
      adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=${view}`,
        sessionToken,
      ),
      adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken),
      // 下拉框的 relation_type 数据源固定拉草稿——不管当前 view 是不是切到已确认，
      // 新增约束这个动作本身只能作用于草稿（后端 add_allowed_combination 也是
      // 校验草稿关系类型），与 spec 第1节/第8节的既有校验口径保持一致。
      adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=draft`, sessionToken),
    ])
    const constraintsData = (await constraintsRes.json()) as { constraints: Constraint[] }
    const termTypesData = (await termTypesRes.json()) as { term_types: TermType[] }
    const relationTypesData = (await relationTypesRes.json()) as { relation_types: RelationType[] }
    setConstraints(constraintsData.constraints)
    setTermTypes(termTypesData.term_types.map((t) => t.value))
    setDraftRelationTypes(relationTypesData.relation_types.map((r) => r.relation_type))
    setLoaded(true)
  }, [sessionToken, tenantId, view])

  useEffect(() => {
    refresh().catch((err) => console.error('约束列表刷新失败', err))
  }, [refresh])

  const constraintKey = (c: Constraint) => `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`

  const handleAdd = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || !subject || !relationType || !object || adding) return
    onError(null)
    setAdding(true)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            subject_term_type: subject,
            relation_type: relationType,
            object_term_type: object,
          }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新增约束失败'))
      }
      setSubject('')
      setRelationType('')
      setObject('')
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '新增约束失败')
    } finally {
      setAdding(false)
    }
  }

  const handleRemove = async (constraint: Constraint) => {
    if (!sessionToken || removingKey !== null) return
    const key = constraintKey(constraint)
    if (!window.confirm('确定要删除这条约束吗？')) return
    onError(null)
    setRemovingKey(key)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints`,
        sessionToken,
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(constraint),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除约束失败'))
      }
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除约束失败')
    } finally {
      setRemovingKey(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <label className="flex items-center gap-2 text-sm font-bold text-ink">
        <input
          type="checkbox"
          checked={view === 'confirmed'}
          onChange={(e) => setView(e.target.checked ? 'confirmed' : 'draft')}
        />
        查看已确认版本（只读）
      </label>

      {loaded && constraints.length === 0 && (
        <p className="text-ink-soft">还没有任何{view === 'draft' ? '草稿' : '已确认的'}约束。</p>
      )}
      {constraints.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className="px-3 py-2">主体类型</th>
                <th className="px-3 py-2">关系类型</th>
                <th className="px-3 py-2">客体类型</th>
                {view === 'draft' && <th className="px-3 py-2">操作</th>}
              </tr>
            </thead>
            <tbody>
              {constraints.map((c) => (
                <tr key={constraintKey(c)} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className="px-3 py-2">{c.subject_term_type}</td>
                  <td className="px-3 py-2 font-mono text-xs">{c.relation_type}</td>
                  <td className="px-3 py-2">{c.object_term_type}</td>
                  {view === 'draft' && (
                    <td className="px-3 py-2">
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleRemove(c)}
                        disabled={removingKey !== null}
                      >
                        {removingKey === constraintKey(c) ? '删除中…' : '删除'}
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'draft' && (
        <form
          onSubmit={handleAdd}
          className="flex flex-wrap items-end gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
        >
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            主体类型
            <select
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              className="border-2 border-ink px-2 py-1.5"
            >
              <option value="">请选择</option>
              {termTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            关系类型（草稿）
            <select
              required
              value={relationType}
              onChange={(e) => setRelationType(e.target.value)}
              className="border-2 border-ink px-2 py-1.5"
            >
              <option value="">请选择</option>
              {draftRelationTypes.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            客体类型
            <select
              required
              value={object}
              onChange={(e) => setObject(e.target.value)}
              className="border-2 border-ink px-2 py-1.5"
            >
              <option value="">请选择</option>
              {termTypes.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            disabled={adding}
            className={`border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
          >
            {adding ? '添加中…' : '+ 添加约束'}
          </button>
        </form>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 产品线 tab
// ---------------------------------------------------------------------------

function ProductLinesTab({
  sessionToken,
  onError,
}: {
  sessionToken: string | null
  onError: (msg: string | null) => void
}) {
  const [items, setItems] = useState<string[]>([])
  const [loaded, setLoaded] = useState(false)
  const [newValue, setNewValue] = useState('')
  const [creating, setCreating] = useState(false)
  const [deletingValue, setDeletingValue] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    const response = await adminFetch('/api/admin/ontology/product-lines', sessionToken)
    const data = (await response.json()) as { product_lines: string[] }
    setItems(data.product_lines)
    setLoaded(true)
  }, [sessionToken])

  useEffect(() => {
    refresh().catch((err) => console.error('产品线列表刷新失败', err))
  }, [refresh])

  const handleAdd = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || !newValue.trim() || creating) return
    onError(null)
    setCreating(true)
    try {
      const response = await adminFetch('/api/admin/ontology/product-lines', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: newValue.trim() }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新增产品线失败'))
      }
      setNewValue('')
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '新增产品线失败')
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (value: string) => {
    if (!sessionToken || deletingValue !== null) return
    if (!window.confirm(`确定要删除产品线「${value}」吗？此操作不可撤销。`)) return
    onError(null)
    setDeletingValue(value)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/product-lines/${encodeURIComponent(value)}`,
        sessionToken,
        { method: 'DELETE' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除产品线失败'))
      }
      await refresh()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除产品线失败')
    } finally {
      setDeletingValue(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="border-2 border-ink bg-accent-yellow px-3 py-2 text-sm text-ink shadow-brutal-sm">
        产品线是全局配置，不属于当前租户，切换租户不影响这个列表。
      </p>

      {loaded && items.length === 0 && <p className="text-ink-soft">还没有定义任何产品线。</p>}
      {items.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className="px-3 py-2">名称</th>
                <th className="px-3 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((value) => (
                <tr key={value} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className="px-3 py-2">{value}</td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                      onClick={() => handleDelete(value)}
                      disabled={deletingValue !== null}
                    >
                      {deletingValue === value ? '删除中…' : '删除'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <form onSubmit={handleAdd} className="flex items-end gap-3 border-2 border-ink bg-card p-4 shadow-brutal">
        <label className="flex flex-col gap-1 text-sm font-bold text-ink">
          产品线名称
          <input
            type="text"
            required
            value={newValue}
            onChange={(e) => setNewValue(e.target.value)}
            className="border-2 border-ink px-2 py-1.5"
          />
        </label>
        <button
          type="submit"
          disabled={creating}
          className={`border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm disabled:opacity-50 ${focusRing}`}
        >
          {creating ? '添加中…' : '+ 添加产品线'}
        </button>
      </form>
    </div>
  )
}
```

在 `frontend/src/App.tsx` 顶部 import 区新增：

```tsx
import { OntologySchemaPage } from './admin/OntologySchemaPage'
```

在 `<Route path="schema-etl" element={<SchemaEtlPage />} />` 之后追加：

```tsx
        <Route path="ontology" element={<OntologySchemaPage />} />
```

在 `frontend/src/admin/AdminLayout.tsx` 的 `<NavLink to="/admin/schema-etl">` 之后追加（放在 ETL 跑批之后、因为逻辑上先定义 schema 再跑 ETL，但两者互相独立，谁在前不影响功能）：

```tsx
            <NavLink to="/admin/ontology" className={navLinkClass}>
              本体 Schema 管理
            </NavLink>
```

- [ ] **Step 2: 手动走查**

启动前端（`npm run dev`）+ 后端，登录管理后台，访问 `/admin/ontology`：
1. 页面加载，顶部显示当前租户和确认状态（草稿中/已确认）。
2. 实体类型 tab：新增一个类型（带2个属性字段），确认列表刷新；编辑改名，确认提示文案出现；删除。
3. 关系类型 tab：确认进入这个 tab 时没有报错（说明 checkout 自动调用成功）；新增一个关系类型；切"查看已确认版本"开关，确认列表变只读；切回草稿，点"确认 schema"，二次确认弹窗文案正确，确认后顶部状态徽章变绿。
4. 约束 tab：下拉框里能选到刚才新增的实体类型和关系类型；新增一条约束；删除。
5. 产品线 tab：确认顶部有"全局配置"提示；新增、删除一个产品线；切换租户下拉框，确认这个 tab 内容不受影响。
6. 关系类型 tab 的"迁移图谱边…"按钮：点开后确认表单和二次确认文案都出现（不强制要求真的有历史边可迁移来验证效果，走查交互本身即可）。
7. 运行 `.venv/Scripts/python.exe -u -m pytest -q tests/api/test_admin_ontology_routes.py -v` 确认后端既有测试仍然全绿（本任务没改后端，这一步是确认走查过程中的任何操作没有意外触发退化）。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx frontend/src/App.tsx frontend/src/admin/AdminLayout.tsx
git commit -m "feat(frontend): add ontology schema management admin page"
```

---

## 完成后

跑 `npx tsc --noEmit`（`frontend/` 目录下）确认类型检查干净；用 `superpowers:subagent-driven-development` 的标准流程做一次全分支终审，重点检查：约束 tab 的下拉框数据源是否真的严格对齐了后端 `_validate_references` 的草稿口径（终审阶段可以补充针对这一点的走查）；四个 tab 之间共享的 `pageError`/`confirmed` 状态在快速切换 tab 时有没有竞态（比如约束 tab 的三个并发请求 `Promise.all` 里任一个失败，`refresh` 会整体抛出未捕获异常——终审阶段确认这个函数外层是否需要包一层 try/catch，还是维持现状让 `useEffect` 里的 `.catch(console.error)` 兜底、只是不会通过 `onError` 展示给用户）。
