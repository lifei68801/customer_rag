import { useCallback, useEffect, useState } from 'react'
import { Scale } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink ${focusRing}`

interface Conflict {
  conflict_id: number
  node_key: string
  field: string
  kept_value: string
  kept_source: string
  incoming_value: string
  incoming_source: string
}

/**
 * 属性值冲突审核。
 *
 * 表格 A 说售价 39、表格 B 说 45——ETL 此前后跑的赢，没有任何人知道发生过
 * 冲突。现在冲突被记下来、库里保留先写的那个值，由这一页来定。
 *
 * 三个动作而不是两个：除了「用 39」「用 45」，还要能手填第三个值。两个来源
 * 都错是可能的（比如两张表都漏了单位），强制二选一等于逼审核员选一个他
 * 已知是错的。
 */
export function AttributeConflictsPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [conflicts, setConflicts] = useState<Conflict[] | null>(null)
  const [total, setTotal] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<number, string>>({})
  const [customValues, setCustomValues] = useState<Record<number, string>>({})
  const [busyId, setBusyId] = useState<number | null>(null)

  useEffect(() => {
    document.title = `${PAGE_TITLES.reviewConflicts} · 管理后台`
  }, [])

  const load = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    setLoadError(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/conflicts`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '冲突列表加载失败'))
      }
      const payload = (await response.json()) as { conflicts: Conflict[]; total: number }
      setConflicts(payload.conflicts)
      setTotal(payload.total)
    } catch (err) {
      // 不退回空数组：「一条冲突都没有」和「列表没拉回来」在界面上长得一样，
      // 而前者会让审核员安心走开，后者该报修。
      setLoadError(err instanceof Error ? err.message : '冲突列表加载失败')
    }
  }, [sessionToken, tenantId])

  useEffect(() => {
    void load()
  }, [load])

  const resolve = async (conflict: Conflict, value: string) => {
    if (!sessionToken || !tenantId) return
    setBusyId(conflict.conflict_id)
    setErrors((prev) => ({ ...prev, [conflict.conflict_id]: '' }))
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/conflicts/${conflict.conflict_id}/resolve`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        // 后端的 503 会说「值已写进术语表，但同步图谱失败」——那句话里有
        // 审核员需要知道的全部信息（这次操作做了一半）。包装成「操作失败」
        // 等于让他以为什么都没发生。
        throw new Error(extractErrorDetail(body, '决议失败'))
      }
      // 成功了才从列表里去掉。失败时留在原地——消失的话审核员以为处理完了。
      setConflicts((prev) => (prev ?? []).filter((c) => c.conflict_id !== conflict.conflict_id))
    } catch (err) {
      setErrors((prev) => ({
        ...prev,
        [conflict.conflict_id]: err instanceof Error ? err.message : '决议失败',
      }))
    } finally {
      setBusyId(null)
    }
  }

  const header = (
    <div className="flex flex-col gap-1">
      <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.reviewConflicts}</h1>
      <p className="text-sm text-ink-soft">
        两次导入对同一个属性给出了不同的值。库里保留的是<strong className="font-bold">先写进去</strong>的那个，
        这里定下最终用哪个。
      </p>
    </div>
  )

  if (loadError !== null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <div role="status" className="flex flex-col items-start gap-3 rounded-card border border-subtle bg-card p-4">
          <p className="text-sm text-status-error-strong">{loadError}</p>
          <button type="button" className={buttonClass} onClick={() => void load()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (conflicts === null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <Skeleton variant="card-list" count={3} />
      </div>
    )
  }

  if (conflicts.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <EmptyState
          icon={Scale}
          title="没有待处理的属性冲突"
          action="多次导入给出不同值时这里才会有东西。眼下每个属性都只有一个说法。"
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {header}
      {total > conflicts.length && (
        // 只列了第一页。不说的话，处理完这 20 条页面会变成「没有待处理的
        // 属性冲突」——而队列里还有几百条，审核员就此收工。
        <p role="status" className="rounded-card border border-subtle bg-card p-3 text-sm text-ink">
          共 {total} 条，这里列了前 {conflicts.length} 条。处理完这一批刷新一下看下一批。
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {conflicts.map((conflict) => (
          <li
            key={conflict.conflict_id}
            data-testid={`conflict-${conflict.conflict_id}`}
            className="flex flex-col gap-3 rounded-card border border-subtle bg-card p-4"
          >
            <p className="font-mono text-sm text-ink">
              {conflict.node_key} · {conflict.field}
            </p>
            <div className="flex flex-wrap gap-2">
              {/* 两个值各自带来源。只给两个数字的话，审核员没有任何依据判断
                  该信哪个——「哪张表更权威」往往就是他做决定的全部依据。 */}
              <button
                type="button"
                className={buttonClass}
                disabled={busyId === conflict.conflict_id}
                onClick={() => void resolve(conflict, conflict.kept_value)}
              >
                用 {conflict.kept_value}（来自 {conflict.kept_source}）
              </button>
              <button
                type="button"
                className={buttonClass}
                disabled={busyId === conflict.conflict_id}
                onClick={() => void resolve(conflict, conflict.incoming_value)}
              >
                用 {conflict.incoming_value}（来自 {conflict.incoming_source}）
              </button>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {/* 第三个入口。两个来源都错是可能的，强制二选一等于逼他选一个
                  已知是错的。 */}
              <input
                aria-label={`手填 ${conflict.field} 的值`}
                className={inputClass}
                value={customValues[conflict.conflict_id] ?? ''}
                onChange={(event) =>
                  setCustomValues((prev) => ({
                    ...prev,
                    [conflict.conflict_id]: event.target.value,
                  }))
                }
                placeholder="两个都不对？填一个"
              />
              <button
                type="button"
                className={buttonClass}
                disabled={
                  busyId === conflict.conflict_id || !customValues[conflict.conflict_id]
                }
                onClick={() =>
                  void resolve(conflict, customValues[conflict.conflict_id] ?? '')
                }
              >
                用这个值
              </button>
            </div>
            {errors[conflict.conflict_id] && (
              <p role="alert" className="text-xs text-status-error-strong">
                {errors[conflict.conflict_id]}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
