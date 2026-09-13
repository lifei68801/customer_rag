import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Spline } from 'lucide-react'
import { EmptyState } from '../EmptyState'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { useConfirm } from '../ConfirmContext'
import { useAdminDensity } from '../DensityContext'
import { Skeleton } from '../Skeleton'
import { useToast } from '../ToastContext'
import { BulkDeleteOutcome, BulkSelectionBar } from '../BulkSelectionBar'
import { useBulkSelection } from '../useBulkSelection'
import { useEditableList } from '../useEditableList'
import { buildBulkDeleteConfirmMessage, requestBulkDelete } from '../bulkDelete'
import type { BulkDeleteResult } from '../bulkDelete'
import type { RelationType, ViewMode } from '../ontologyTypes'
import { BULK_SCOPE, bulkKeys, emptyRelationTypeDraft, focusRing } from './shared'

export function RelationTypesTab({
  sessionToken,
  tenantId,
  onError,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [items, setItems] = useState<RelationType[]>([])
  const [loaded, setLoaded] = useState(false)
  // 跟实体类型 tab 共用同一个编辑状态机，见 useEditableList 的模块文档。
  const list = useEditableList()
  const [draft, setDraft] = useState<RelationType>(emptyRelationTypeDraft())
  const [migrateTarget, setMigrateTarget] = useState('')

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
  }, [refresh, confirmVersion])

  const startEdit = (item: RelationType) => {
    list.openEdit(item.relation_type)
    setDraft({ ...item })
  }

  const startCreate = () => {
    list.openCreate()
    setDraft(emptyRelationTypeDraft())
  }

  const cancelEdit = () => {
    list.close()
    setDraft(emptyRelationTypeDraft())
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    const form = list.form
    if (!sessionToken || (form.kind !== 'creating' && form.kind !== 'editing')) return
    const isCreate = form.kind === 'creating'
    const key = isCreate ? '' : form.key
    onError(null)
    await list.run('save', key, async () => {
      try {
        const url = isCreate
          ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types`
          : `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/${encodeURIComponent(key)}`
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
          throw new Error(
            extractErrorDetail(body, isCreate ? '新增关系类型失败' : '更新关系类型失败'),
          )
        }
        cancelEdit()
        await refresh()
        onDataChanged()
      } catch (err) {
        onError(err instanceof Error ? err.message : '保存失败')
      }
    })
  }

  const bulk = useBulkSelection()
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [bulkOutcome, setBulkOutcome] = useState<BulkDeleteResult | null>(null)
  const listedKeys = items.map((item) => item.relation_type)

  const handleBulkDelete = async () => {
    const target = bulk.targetFor(BULK_SCOPE)
    if (!sessionToken || target === null || bulkDeleting) return
    if (!(await confirm(buildBulkDeleteConfirmMessage(target, '关系类型')))) return
    onError(null)
    setBulkDeleting(true)
    try {
      const result = await requestBulkDelete(
        sessionToken,
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/bulk-delete`,
        target,
        (t) => ({ relation_types: bulkKeys(t) }),
      )
      setBulkOutcome(result)
      bulk.clear()
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '批量删除失败')
    } finally {
      setBulkDeleting(false)
    }
  }

  const handleDelete = async (relationType: string) => {
    if (!sessionToken || list.busy) return
    if (!(await confirm(`确定要删除关系类型「${relationType}」吗？此操作不可撤销。`))) return
    onError(null)
    await list.run('delete', relationType, async () => {
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
        showToast('已删除关系类型')
        await refresh()
        onDataChanged()
      } catch (err) {
        onError(err instanceof Error ? err.message : '删除失败')
      }
    })
  }

  const handleMigrate = async (event: FormEvent) => {
    event.preventDefault()
    const form = list.form
    if (!sessionToken || form.kind !== 'migrating' || list.busy) return
    const from = form.from
    if (
      !(await confirm(
        `这会遍历租户「${tenantId}」在 Neo4j 图谱里所有类型为「${from}」的边，批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      ))
    ) {
      return
    }
    onError(null)
    await list.run('migrate', from, async () => {
      try {
        const response = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/relation-types/migrate`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_type: from, new_type: migrateTarget }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '迁移图谱边失败'))
        }
        const data = (await response.json()) as { migrated_count: number }
        showToast(`已迁移 ${data.migrated_count} 条边`)
        list.close()
        setMigrateTarget('')
      } catch (err) {
        onError(err instanceof Error ? err.message : '迁移图谱边失败')
      }
    })
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {!loaded && <Skeleton variant="table-rows" count={4} />}
      {loaded && items.length === 0 && (
        <EmptyState
          icon={Spline}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}关系类型`}
          action={
            view === 'draft'
              ? '点击下方「+ 新增关系类型」创建一个。关系类型定义实体之间可以有哪些连接。'
              : '草稿确认之后，已确认的关系类型会出现在这里。'
          }
        />
      )}
      {bulkOutcome !== null && (
        <BulkDeleteOutcome
          result={bulkOutcome}
          noun="关系类型"
          onDismiss={() => setBulkOutcome(null)}
        />
      )}
      {view === 'draft' && items.length > 0 && (
        <BulkSelectionBar
          scopeId={BULK_SCOPE}
          listedKeys={listedKeys}
          total={listedKeys.length}
          filters={{}}
          noun="关系类型"
          scopeLabel="全部"
          selection={bulk}
          onDelete={handleBulkDelete}
          deleting={bulkDeleting}
        />
      )}
      {items.length > 0 && (
        <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-subtle bg-paper text-ink">
                {view === 'draft' && <th className={cellPadding} aria-label="选中" />}
                <th className={cellPadding}>关系类型</th>
                <th className={cellPadding}>示例短语</th>
                <th className={cellPadding}>说明</th>
                <th className={cellPadding}>支持链式查询</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.relation_type} className="border-b border-subtle text-ink last:border-b-0">
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <input
                        type="checkbox"
                        checked={bulk.isSelected(BULK_SCOPE, item.relation_type)}
                        onChange={() => bulk.toggleKey(BULK_SCOPE, item.relation_type)}
                        aria-label={`选中关系类型 ${item.relation_type}`}
                        className={`h-4 w-4 cursor-pointer ${focusRing}`}
                      />
                    </td>
                  )}
                  <td className={`${cellPadding} font-mono text-xs`}>{item.relation_type}</td>
                  <td className={cellPadding}>{item.example_phrase}</td>
                  <td className={cellPadding}>{item.description || '-'}</td>
                  <td className={cellPadding}>{item.allow_chain_query ? '是' : '否'}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        改名/编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => {
                          list.openMigrate(item.relation_type)
                          setMigrateTarget('')
                        }}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        迁移图谱边…
                      </button>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleDelete(item.relation_type)}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        {list.isDeleting(item.relation_type) ? '删除中…' : '删除'}
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

      {view === 'draft' && list.form.kind === 'idle' && (
        <button
          type="button"
          onClick={startCreate}
          className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新增关系类型
        </button>
      )}

      {view === 'draft' &&
        (list.form.kind === 'creating' || list.form.kind === 'editing') && (
        <form onSubmit={submit} className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            关系类型名（大写字母/数字/下划线）
            <input
              type="text"
              required
              value={draft.relation_type}
              onChange={(e) => setDraft((prev) => ({ ...prev, relation_type: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink focus:outline-none ${focusRing}`}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            示例短语
            <input
              type="text"
              required
              value={draft.example_phrase}
              onChange={(e) => setDraft((prev) => ({ ...prev, example_phrase: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            说明
            <input
              type="text"
              value={draft.description}
              onChange={(e) => setDraft((prev) => ({ ...prev, description: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
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
              disabled={list.busy}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {list.busy ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}

      {list.form.kind === 'migrating' && (
        <form
          onSubmit={handleMigrate}
          className="flex flex-col gap-3 rounded-panel border border-status-error bg-card p-4"
        >
          <p className="text-sm text-ink">
            把租户「{tenantId}」图谱里所有类型为「{list.form.from}」的边迁移成：
          </p>
          <select
            required
            value={migrateTarget}
            onChange={(e) => setMigrateTarget(e.target.value)}
            aria-label="迁移目标类型"
            className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink focus:outline-none ${focusRing}`}
          >
            <option value="">请选择新类型</option>
            {items
              .filter(
                (item) => list.form.kind !== 'migrating' || item.relation_type !== list.form.from,
              )
              .map((item) => (
                <option key={item.relation_type} value={item.relation_type}>
                  {item.relation_type}
                </option>
              ))}
          </select>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={list.busy}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 py-2 text-sm font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {list.busy ? '迁移中…' : '确认迁移'}
            </button>
            <button
              type="button"
              onClick={() => {
                list.close()
              }}
              className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
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

