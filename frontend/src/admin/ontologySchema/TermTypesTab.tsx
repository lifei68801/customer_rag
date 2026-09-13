import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { Boxes } from 'lucide-react'
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
import { valueTypeOptionLabel } from '../extraFieldDisplay'
import type { ExtraFieldSpec, TermType, ViewMode } from '../ontologyTypes'
import { BULK_SCOPE, STANDARD_NAME_VALUE_TYPES, VALUE_TYPES, bulkKeys, emptyTermTypeDraft, focusRing } from './shared'

export function TermTypesTab({
  sessionToken,
  tenantId,
  onError,
  onDeleteBlocked,
  view,
  confirmVersion,
  onDataChanged,
}: {
  sessionToken: string | null
  tenantId: string
  onError: (msg: string | null) => void
  /** 删除被"分类仍在用"挡住时，把挡路的实体类型名交回页面级，用来生成
   * "去实体明细按这个类型筛出来"的链接。 */
  onDeleteBlocked: (termType: string) => void
  view: ViewMode
  confirmVersion: number
  onDataChanged: () => void
}) {
  const confirm = useConfirm()
  const showToast = useToast()
  const { density } = useAdminDensity()
  const [items, setItems] = useState<TermType[]>([])
  const [loaded, setLoaded] = useState(false)
  // 表单开着哪个、有没有请求在飞，都归 useEditableList 管——「同一时刻只能
  // 有一个操作在飞」这条不变量此前散在十几个 disabled 表达式里，而且并不
  // 一致（删除在飞时迁移按钮照样能点）。草稿和迁移目标是数据不是状态机，
  // 留在这里。
  const list = useEditableList()
  const [draft, setDraft] = useState<TermType>(emptyTermTypeDraft())
  const [migrateTarget, setMigrateTarget] = useState('')

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      // checkout 对用户透明——每次进这个 tab / 切换视图前，先确保草稿存在，
      // 幂等操作，已有草稿时后端直接跳过（见 app/graphrag/ontology_lifecycle.py
      // ::checkout_draft 的说明）。实体类型的编辑/删除严格只作用于 status='draft'
      // 行（见 ontology_categories.py::update_term_type/delete_term_type），没有
      // 草稿就编辑会报"草稿里不存在分类"，所以这里跟关系类型/约束 tab 一样
      // 主动 checkout，不能只依赖页面级那次（有竞态）。
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
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types?status=${view}`,
        sessionToken,
      )
      const data = (await response.json()) as { term_types: TermType[] }
      setItems(data.term_types)
    } catch (err) {
      onError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, view, onError])

  useEffect(() => {
    refresh().catch((err) => console.error('实体类型列表刷新失败', err))
  }, [refresh, confirmVersion])

  const startEdit = (item: TermType) => {
    list.openEdit(item.value)
    setDraft({ ...item, extra_fields: item.extra_fields.map((f) => ({ ...f })) })
  }

  const startCreate = () => {
    list.openCreate()
    setDraft(emptyTermTypeDraft())
  }

  const cancelEdit = () => {
    list.close()
    setDraft(emptyTermTypeDraft())
  }

  const addField = () => {
    setDraft((prev) => ({
      ...prev,
      extra_fields: [...prev.extra_fields, { name: '', value_type: 'string', label: '' }],
    }))
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
    const form = list.form
    if (!sessionToken || (form.kind !== 'creating' && form.kind !== 'editing')) return
    const isCreate = form.kind === 'creating'
    const key = isCreate ? '' : form.key
    onError(null)
    await list.run('save', key, async () => {
      try {
        const url = isCreate
          ? `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types`
          : `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(key)}`
        const response = await adminFetch(url, sessionToken, {
          method: isCreate ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(draft),
        })
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(
            extractErrorDetail(body, isCreate ? '新增实体类型失败' : '更新实体类型失败'),
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
  const listedKeys = items.map((item) => item.value)

  const handleBulkDelete = async () => {
    const target = bulk.targetFor(BULK_SCOPE)
    if (!sessionToken || target === null || bulkDeleting) return
    if (!(await confirm(buildBulkDeleteConfirmMessage(target, '实体类型')))) return
    onError(null)
    setBulkDeleting(true)
    try {
      const result = await requestBulkDelete(
        sessionToken,
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/bulk-delete`,
        target,
        (t) => ({ values: bulkKeys(t) }),
      )
      // 结果面板留在页面上而不是弹个 toast：删 10 个类型有 8 个被引用
      // 守卫挡住是这张表的常态，那 8 条是谁挡的才是用户接下来要处理的
      // 东西，一闪而过就等于没报。
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

  const handleDelete = async (value: string) => {
    if (!sessionToken || list.busy) return
    if (!(await confirm(`确定要删除实体类型「${value}」吗？此操作不可撤销。`))) return
    onError(null)
    await list.run('delete', value, async () => {
      try {
        const response = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/${encodeURIComponent(value)}`,
          sessionToken,
          { method: 'DELETE' },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          // 409 会带一份结构化的挡路术语（见 app/api/admin_ontology_routes.py
          // ::delete_term_type_category）。有 node_keys 才说明真的是被术语挡住
          // 的——只被草稿约束挡住时实体明细里没东西可处理，不该给这条链接。
          const blocking = (
            body as { blocking_terms?: { term_type?: string; node_keys?: string[] } }
          ).blocking_terms
          if (blocking?.term_type && (blocking.node_keys?.length ?? 0) > 0) {
            onDeleteBlocked(blocking.term_type)
          }
          throw new Error(extractErrorDetail(body, '删除实体类型失败'))
        }
        showToast('已删除实体类型')
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
        `这会把租户「${tenantId}」所有 term_type 为「${from}」的真实术语和图谱节点批量改成「${migrateTarget}」，不可逆。确定要继续吗？`,
      ))
    ) {
      return
    }
    onError(null)
    await list.run('migrate', from, async () => {
      try {
        const response = await adminFetch(
          `/api/admin/ontology/${encodeURIComponent(tenantId)}/term-types/migrate`,
          sessionToken,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_type: from, new_type: migrateTarget }),
          },
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '迁移实体类型失败'))
        }
        const data = (await response.json()) as {
          terms_migrated: number
          graph_nodes_migrated: number
        }
        showToast(`已迁移 ${data.terms_migrated} 条术语、${data.graph_nodes_migrated} 个图谱节点`)
        list.close()
        setMigrateTarget('')
      } catch (err) {
        onError(err instanceof Error ? err.message : '迁移实体类型失败')
      }
    })
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {!loaded && <Skeleton variant="table-rows" count={4} />}
      {loaded && items.length === 0 && list.form.kind === 'idle' && (
        <EmptyState
          icon={Boxes}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}实体类型`}
          action={
            view === 'draft'
              ? '点击下方「+ 新增实体类型」创建一个。实体类型是本体的骨架，关系和约束都建立在它之上。'
              : '草稿确认之后，已确认的实体类型会出现在这里。'
          }
        />
      )}
      {bulkOutcome !== null && (
        <BulkDeleteOutcome
          result={bulkOutcome}
          noun="实体类型"
          onDismiss={() => setBulkOutcome(null)}
        />
      )}
      {view === 'draft' && items.length > 0 && (
        <BulkSelectionBar
          scopeId={BULK_SCOPE}
          listedKeys={listedKeys}
          total={listedKeys.length}
          filters={{}}
          noun="实体类型"
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
                <th className={cellPadding}>类型名</th>
                <th className={cellPadding}>属性字段数</th>
                <th className={cellPadding}>自身取值类型</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.value} className="border-b border-subtle text-ink last:border-b-0">
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <input
                        type="checkbox"
                        checked={bulk.isSelected(BULK_SCOPE, item.value)}
                        onChange={() => bulk.toggleKey(BULK_SCOPE, item.value)}
                        aria-label={`选中实体类型 ${item.value}`}
                        className={`h-4 w-4 cursor-pointer ${focusRing}`}
                      />
                    </td>
                  )}
                  <td className={cellPadding}>{item.value}</td>
                  <td className={cellPadding}>{item.extra_fields.length}</td>
                  <td className={cellPadding}>{item.standard_name_value_type}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => startEdit(item)}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        编辑
                      </button>
                      <button
                        type="button"
                        className={`mr-2 font-bold underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => {
                          list.openMigrate(item.value)
                          setMigrateTarget('')
                        }}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        迁移实体类型…
                      </button>
                      <button
                        type="button"
                        className={`font-bold text-status-error underline disabled:opacity-50 ${focusRing}`}
                        onClick={() => handleDelete(item.value)}
                        disabled={list.form.kind !== 'idle' || list.busy}
                      >
                        {list.isDeleting(item.value) ? '删除中…' : '删除'}
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
          直接改名后旧类型不再有草稿行可选，「迁移实体类型」用不了；要迁移真实数据，
          请新增一个类型作为新条目、确认草稿后用「迁移实体类型」把旧类型迁到新类型，再删除旧条目。
        </p>
      )}

      {view === 'draft' && list.form.kind === 'idle' && (
        <button
          type="button"
          onClick={startCreate}
          className={`min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新增实体类型
        </button>
      )}

      {view === 'draft' &&
        (list.form.kind === 'creating' || list.form.kind === 'editing') && (
        <form onSubmit={submit} className="flex flex-col gap-3 rounded-panel border border-subtle bg-card p-4">
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            类型名
            <input
              type="text"
              required
              value={draft.value}
              onChange={(e) => setDraft((prev) => ({ ...prev, value: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            自身取值类型
            <select
              value={draft.standard_name_value_type}
              aria-label="自身取值类型"
              onChange={(e) => setDraft((prev) => ({ ...prev, standard_name_value_type: e.target.value }))}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
            >
              {STANDARD_NAME_VALUE_TYPES.map((t) => (
                <option key={t} value={t}>{valueTypeOptionLabel(t)}</option>
              ))}
            </select>
            <span className="text-xs font-normal text-ink-soft">
              这个类型的实例本身代表什么类型的值（比如"销量""收入"这类每个取值都是独立节点的类型，
              应该选"小数"，才能用"大于/小于"做区间查询；大多数类型（产品名、公司名…）保持默认的"文本"即可）
            </span>
          </label>

          <div className="flex flex-col gap-2">
            <span className="text-sm font-bold text-ink">属性字段</span>
            <span className="text-xs font-normal text-ink-soft">
              每个属性有两个名字：显示名给人看（界面、问答里显示，可以写中文，比如「售价」），
              字段名给系统用（存储、索引和结构化查询用它，只能是字母/数字/下划线，比如 price）。
              显示名留空的话，界面上就直接显示字段名。
            </span>
            {draft.extra_fields.map((field, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2">
                <input
                  type="text"
                  placeholder="显示名，如 售价"
                  aria-label="字段显示名"
                  value={field.label ?? ''}
                  onChange={(e) => updateField(index, { label: e.target.value })}
                  className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                />
                <input
                  type="text"
                  placeholder="字段名，如 price"
                  aria-label="字段名"
                  required
                  value={field.name}
                  onChange={(e) => updateField(index, { name: e.target.value })}
                  className={`rounded-control border border-subtle bg-paper px-2 py-1.5 font-mono text-ink placeholder:text-ink-soft focus:outline-none ${focusRing}`}
                />
                <select
                  value={field.value_type}
                  aria-label="字段类型"
                  onChange={(e) => updateField(index, { value_type: e.target.value })}
                  className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
                >
                  {VALUE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {valueTypeOptionLabel(t)}
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
              className={`self-start rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              + 添加字段
            </button>
          </div>

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
            把租户「{tenantId}」所有 term_type 为「{list.form.from}」的真实术语和图谱节点迁移成：
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
              .filter((item) => list.form.kind !== 'migrating' || item.value !== list.form.from)
              .map((item) => (
                <option key={item.value} value={item.value}>
                  {item.value}
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
// 关系类型 tab
// ---------------------------------------------------------------------------

