import { useState } from 'react'
import type { FormEvent } from 'react'
import { ShieldCheck, Waypoints } from 'lucide-react'
import { Link } from 'react-router-dom'
import { EmptyState } from '../EmptyState'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { useConfirm } from '../ConfirmContext'
import { useAdminDensity } from '../DensityContext'
import { Skeleton } from '../Skeleton'
import { useToast } from '../ToastContext'
import { BulkDeleteOutcome, BulkSelectionBar } from '../BulkSelectionBar'
import { useBulkSelection } from '../useBulkSelection'
import { buildBulkDeleteConfirmMessage, requestBulkDelete } from '../bulkDelete'
import type { BulkDeleteResult } from '../bulkDelete'
import { useOntologyData } from '../useOntologyData'
import { ADMIN_ROUTES } from '../../adminRoutes'
import type { Constraint, ViewMode } from '../ontologyTypes'
import { BULK_SCOPE, bulkKeys, focusRing } from './shared'

export function ConstraintsTab({
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
  const [subject, setSubject] = useState('')
  const [relationType, setRelationType] = useState('')
  const [object, setObject] = useState('')
  const [adding, setAdding] = useState(false)
  const [removingKey, setRemovingKey] = useState<string | null>(null)
  // 约束本质是 (主语类型, 关系, 宾语类型) 的边表——图和表是同一份数据的两种
  // 呈现。默认给表：新增/删除都在表上操作，图是只读的全局视图。

  const { constraints, termTypes, draftRelationTypes, loaded, refresh } =
    useOntologyData({
      sessionToken,
      tenantId,
      view,
      withGraphOverlay: false,
      reloadKey: confirmVersion,
      onError,
    })

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
      showToast('已添加约束')
      setSubject('')
      setRelationType('')
      setObject('')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '新增约束失败')
    } finally {
      setAdding(false)
    }
  }

  const bulk = useBulkSelection()
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [bulkOutcome, setBulkOutcome] = useState<BulkDeleteResult | null>(null)
  const listedKeys = constraints.map(constraintKey)

  const handleBulkDelete = async () => {
    const target = bulk.targetFor(BULK_SCOPE)
    if (!sessionToken || target === null || bulkDeleting) return
    if (!(await confirm(buildBulkDeleteConfirmMessage(target, '约束')))) return
    onError(null)
    setBulkDeleting(true)
    try {
      const byKey = new Map(constraints.map((c) => [constraintKey(c), c]))
      const result = await requestBulkDelete(
        sessionToken,
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/constraints/bulk-delete`,
        target,
        // 约束没有单列的主键，服务端要的是三元组本身；选中状态里那个 key
        // 只是这张表内部用来认行的。
        (t) => ({
          constraints: bulkKeys(t)
            .map((key) => byKey.get(key))
            .filter((c): c is Constraint => c !== undefined),
        }),
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

  const handleRemove = async (constraint: Constraint) => {
    if (!sessionToken || removingKey !== null) return
    const key = constraintKey(constraint)
    if (!(await confirm('确定要删除这条约束吗？'))) return
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
      showToast('已删除约束')
      await refresh()
      onDataChanged()
    } catch (err) {
      onError(err instanceof Error ? err.message : '删除约束失败')
    } finally {
      setRemovingKey(null)
    }
  }

  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'

  return (
    <div className="flex flex-col gap-4">
      {loaded && (
        <Link
          to={ADMIN_ROUTES.ontologyGraph}
          className={`inline-flex min-h-[36px] items-center gap-1.5 self-start rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
        >
          <Waypoints aria-hidden="true" className="h-4 w-4" />
          在本体图中查看
        </Link>
      )}
      {!loaded && <Skeleton variant="table-rows" count={3} />}
      {loaded && constraints.length === 0 && (
        <EmptyState
          icon={ShieldCheck}
          title={`还没有任何${view === 'draft' ? '草稿' : '已确认的'}约束`}
          action={
            view === 'draft'
              ? '在下方表单里添加一个。约束声明「哪个实体类型可以经哪种关系连到哪个实体类型」，是本体图上的边。'
              : '草稿确认之后，已确认的约束会出现在这里。'
          }
        />
      )}
      {bulkOutcome !== null && (
        <BulkDeleteOutcome
          result={bulkOutcome}
          noun="约束"
          onDismiss={() => setBulkOutcome(null)}
        />
      )}
      {view === 'draft' && constraints.length > 0 && (
        <BulkSelectionBar
          scopeId={BULK_SCOPE}
          listedKeys={listedKeys}
          total={listedKeys.length}
          filters={{}}
          noun="约束"
          scopeLabel="全部"
          selection={bulk}
          onDelete={handleBulkDelete}
          deleting={bulkDeleting}
        />
      )}
      {constraints.length > 0 && (
        <div className="overflow-x-auto overflow-y-hidden rounded-card border border-subtle bg-card">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-subtle bg-paper text-ink">
                {view === 'draft' && <th className={cellPadding} aria-label="选中" />}
                <th className={cellPadding}>主体类型</th>
                <th className={cellPadding}>关系类型</th>
                <th className={cellPadding}>客体类型</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {constraints.map((c) => (
                <tr key={constraintKey(c)} className="border-b border-subtle text-ink last:border-b-0">
                  {view === 'draft' && (
                    <td className={cellPadding}>
                      <input
                        type="checkbox"
                        checked={bulk.isSelected(BULK_SCOPE, constraintKey(c))}
                        onChange={() => bulk.toggleKey(BULK_SCOPE, constraintKey(c))}
                        aria-label={`选中约束 ${c.subject_term_type} -${c.relation_type}-> ${c.object_term_type}`}
                        className={`h-4 w-4 cursor-pointer ${focusRing}`}
                      />
                    </td>
                  )}
                  <td className={cellPadding}>{c.subject_term_type}</td>
                  <td className={`${cellPadding} font-mono text-xs`}>{c.relation_type}</td>
                  <td className={cellPadding}>{c.object_term_type}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
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
          className="flex flex-wrap items-end gap-3 rounded-panel border border-subtle bg-card p-4"
        >
          <label className="flex flex-col gap-1 text-sm font-bold text-ink">
            主体类型
            <select
              required
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
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
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
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
              className={`rounded-control border border-subtle bg-paper px-2 py-1.5 text-ink focus:outline-none ${focusRing}`}
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
            className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
          >
            {adding ? '添加中…' : '+ 添加约束'}
          </button>
        </form>
      )}
    </div>
  )
}
