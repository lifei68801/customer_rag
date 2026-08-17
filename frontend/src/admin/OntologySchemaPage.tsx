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
        <button
          type="button"
          className={tabButtonClass(tab === 'term-types')}
          onClick={() => {
            setTab('term-types')
            setPageError(null)
          }}
        >
          实体类型
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'relation-types')}
          onClick={() => {
            setTab('relation-types')
            setPageError(null)
          }}
        >
          关系类型
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'constraints')}
          onClick={() => {
            setTab('constraints')
            setPageError(null)
          }}
        >
          约束
        </button>
        <button
          type="button"
          className={tabButtonClass(tab === 'product-lines')}
          onClick={() => {
            setTab('product-lines')
            setPageError(null)
          }}
        >
          产品线
        </button>
      </nav>

      {tab === 'term-types' && (
        <TermTypesTab
          key={tenantId}
          sessionToken={sessionToken}
          tenantId={tenantId}
          onError={setPageError}
        />
      )}
      {tab === 'relation-types' && (
        <RelationTypesTab
          key={tenantId}
          sessionToken={sessionToken}
          tenantId={tenantId}
          onError={setPageError}
          onConfirmed={refreshStatus}
        />
      )}
      {tab === 'constraints' && (
        <ConstraintsTab key={tenantId} sessionToken={sessionToken} tenantId={tenantId} onError={setPageError} />
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
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`,
        sessionToken,
      )
      const data = (await response.json()) as { term_types: TermType[] }
      setItems(data.term_types)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, onError])

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
      {!loaded && <p className="text-ink-soft">加载中…</p>}
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
                      className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                      onClick={() => startEdit(item)}
                      disabled={editingValue !== null}
                    >
                      编辑
                    </button>
                    <button
                      type="button"
                      className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                      onClick={() => handleDelete(item.value)}
                      disabled={deletingValue !== null || editingValue !== null}
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
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none ${focusRing}`}
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
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
              className="border-2 border-ink bg-paper px-2 py-1.5 font-mono text-ink focus:shadow-brutal focus:outline-none"
            />
          </label>

          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-ink">属性字段</span>
            {draft.extra_fields.map((field, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  placeholder="字段名"
                  aria-label="字段名"
                  required
                  value={field.name}
                  onChange={(e) => updateField(index, { name: e.target.value })}
                  className="border-2 border-ink bg-paper px-2 py-1.5 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
                />
                <select
                  value={field.value_type}
                  aria-label="字段类型"
                  onChange={(e) => updateField(index, { value_type: e.target.value })}
                  className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
              className={`self-start border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              + 添加字段
            </button>
          </div>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || savingValue !== null}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingValue !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
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
  const [migrateSuccessMessage, setMigrateSuccessMessage] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      // checkout 对用户透明——每次进这个 tab / 切换视图前，先确保草稿存在，
      // 幂等操作，已有草稿时后端直接跳过（见 app/graphrag/ontology_lifecycle.py
      // ::checkout_draft 的说明）。
      const checkoutResponse = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
        sessionToken,
        { method: 'POST' },
      )
      if (!checkoutResponse.ok) {
        const body = await checkoutResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, 'schema 草稿初始化失败'))
      }
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=${view}`,
        sessionToken,
      )
      const data = (await response.json()) as { relation_types: RelationType[] }
      setItems(data.relation_types)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

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
      setMigrateSuccessMessage(`已迁移 ${data.migrated_count} 条边`)
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
            className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-green px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
          >
            {confirming ? '确认中…' : '确认 schema'}
          </button>
        )}
      </div>

      {!loaded && <p className="text-ink-soft">加载中…</p>}
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
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={editingType !== null}
                      >
                        改名/编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => {
                          setMigratingFrom(item.relation_type)
                          setMigrateTarget('')
                          setMigrateSuccessMessage(null)
                        }}
                        disabled={migrating}
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
          className={`min-h-[44px] cursor-pointer self-start border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none ${focusRing}`}
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
              className="border-2 border-ink bg-paper px-2 py-1.5 font-mono text-ink focus:shadow-brutal focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            示例短语
            <input
              type="text"
              required
              value={draft.example_phrase}
              onChange={(e) => setDraft((prev) => ({ ...prev, example_phrase: e.target.value }))}
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            说明
            <input
              type="text"
              value={draft.description}
              onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))}
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating || savingType !== null ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {migrateSuccessMessage && <p className="text-sm text-ink">{migrateSuccessMessage}</p>}

      {migratingFrom !== null && (
        <form
          onSubmit={handleMigrate}
          className="flex flex-col gap-3 border-2 border-status-error bg-card p-4 shadow-brutal"
        >
          <p className="text-sm text-ink">
            把租户「{tenantId}」图谱里所有类型为「{migratingFrom}」的边迁移成：
          </p>
          <select
            required
            value={migrateTarget}
            onChange={(e) => setMigrateTarget(e.target.value)}
            aria-label="迁移目标类型"
            className="border-2 border-ink bg-paper px-2 py-1.5 font-mono text-ink focus:shadow-brutal focus:outline-none"
          >
            <option value="">请选择新类型</option>
            {items
              .filter((item) => item.relation_type !== migratingFrom)
              .map((item) => (
                <option key={item.relation_type} value={item.relation_type}>
                  {item.relation_type}
                </option>
              ))}
          </select>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={migrating}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-status-error px-4 py-2 text-sm font-bold text-white shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {migrating ? '迁移中…' : '确认迁移'}
            </button>
            <button
              type="button"
              onClick={() => {
                setMigratingFrom(null)
                setMigrateSuccessMessage(null)
              }}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-4 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
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
    try {
      const checkoutResponse = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/checkout`,
        sessionToken,
        { method: 'POST' },
      )
      if (!checkoutResponse.ok) {
        const body = await checkoutResponse.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, 'schema 草稿初始化失败'))
      }
      const [constraintsRes, termTypesRes, relationTypesRes] = await Promise.all([
        adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints?status=${view}`,
          sessionToken,
        ),
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`, sessionToken),
        // 下拉框的 relation_type 数据源固定拉草稿——不管当前 view 是不是切到已确认，
        // 新增约束这个动作本身只能作用于草稿（后端 add_allowed_combination 也是
        // 校验草稿关系类型），与后端 ontology_constraints.py::_validate_references
        // 的既有校验口径保持一致。
        adminFetch(`/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types?status=draft`, sessionToken),
      ])
      const constraintsData = (await constraintsRes.json()) as { constraints: Constraint[] }
      const termTypesData = (await termTypesRes.json()) as { term_types: TermType[] }
      const relationTypesData = (await relationTypesRes.json()) as { relation_types: RelationType[] }
      setConstraints(constraintsData.constraints)
      setTermTypes(termTypesData.term_types.map((t) => t.value))
      setDraftRelationTypes(relationTypesData.relation_types.map((r) => r.relation_type))
      setLoaded(true)
    } catch (err) {
      // Promise.all 里任一并发请求失败都会在这里被捕获——四个 tab 里这是唯一一个
      // 发多个并发请求的 tab，其余三个 tab 的 refresh() 各自只有一次 fetch，
      // 用同样的 try/catch/finally 模式即可覆盖单个请求失败的情况。
      onError(err instanceof Error ? err.message : '约束列表刷新失败')
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

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

      {!loaded && <p className="text-ink-soft">加载中…</p>}
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
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
              className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
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
            className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
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
    try {
      const response = await adminFetch('/api/admin/ontology/product-lines', sessionToken)
      const data = (await response.json()) as { product_lines: string[] }
      setItems(data.product_lines)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, onError])

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

      {!loaded && <p className="text-ink-soft">加载中…</p>}
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
            className="border-2 border-ink bg-paper px-2 py-1.5 text-ink focus:shadow-brutal focus:outline-none"
          />
        </label>
        <button
          type="submit"
          disabled={creating || !newValue.trim()}
          className={`min-h-[44px] cursor-pointer border-2 border-ink bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {creating ? '添加中…' : '+ 添加产品线'}
        </button>
      </form>
    </div>
  )
}
