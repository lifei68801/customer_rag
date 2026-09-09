import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export interface Domain {
  tenant_id: string
  name: string
  org_id: string | null
  org_name: string | null
}

interface DomainStats {
  tenant_id: string
  term_count: number
  edge_count: number
  document_count: number
  pending_review_count: number
}

/**
 * 一个领域一张卡。**自己取数、自己出骨架屏、自己失败。**
 *
 * 这是 spec D5 裁决二的落点：清单端点不带数字，每张卡各自请求自己的统计。
 * 由 DashboardPage 统一 `Promise.all` 再一次性渲染的话，第一张卡也要等最慢
 * 的那个领域算完——而每个领域的边计数都是一次图查询。
 *
 * 失败也是各自的：一个领域的图谱查不通，不该让整个看板变成一个错误页。
 * 别的领域的数字是好的，凭什么一起看不到。
 */
export function DomainCard({ domain }: { domain: Domain }) {
  const { sessionToken } = useAdminAuth()
  const { switchTenant } = useAdminTenant()
  const navigate = useNavigate()
  const [stats, setStats] = useState<DomainStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [going, setGoing] = useState(false)

  const load = useCallback(async () => {
    if (!sessionToken) return
    setError(null)
    setStats(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(domain.tenant_id)}/dashboard/stats`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '统计没算出来'))
      }
      setStats((await response.json()) as DomainStats)
    } catch (err) {
      setError(err instanceof Error ? err.message : '统计没算出来')
    }
  }, [sessionToken, domain.tenant_id])

  useEffect(() => {
    void load()
  }, [load])

  /**
   * 去这个领域的某一页。
   *
   * **先把当前租户切过去，切成功了才跳。** 看板是跨领域的：用户当前挂着
   * A、点的是 B 那张卡，直接跳的话他落在目标页上看到的是 A 的数据——数字
   * 是 B 的、内容是 A 的，而界面全程不说话。
   *
   * 切换失败时不跳（switchTenant 内部已经弹了 toast 说原因）：跳过去只会
   * 让他在错误的租户里操作。
   */
  const goTo = async (path: string) => {
    setGoing(true)
    try {
      if (await switchTenant(domain.tenant_id)) navigate(path)
    } finally {
      setGoing(false)
    }
  }

  const shell = (children: React.ReactNode) => (
    <section
      data-testid={`domain-card-${domain.tenant_id}`}
      className="flex flex-col gap-3 rounded-card border border-subtle bg-card p-4"
    >
      <h3 className="font-mono text-sm font-semibold text-ink">{domain.name}</h3>
      {children}
    </section>
  )

  if (error !== null) {
    return shell(
      <div className="flex flex-col items-start gap-2">
        <p role="status" className="text-sm text-status-error-strong">
          {error}
        </p>
        {/* 只说坏了不给出路等于只做了一半。 */}
        <button type="button" className={buttonClass} onClick={() => void load()}>
          重试
        </button>
      </div>,
    )
  }

  if (stats === null) {
    return shell(
      // 骨架屏而不是白屏：领域名已经在了，卡片轮廓也在，只有数字还没到。
      // 一屏空白读起来像页面没加载出来。
      <div
        data-testid={`domain-card-${domain.tenant_id}-skeleton`}
        aria-hidden="true"
        className="grid grid-cols-2 gap-3"
      >
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-10 animate-pulse rounded-control bg-interactive-hover" />
        ))}
      </div>,
    )
  }

  const isEmpty =
    stats.term_count === 0 && stats.edge_count === 0 && stats.document_count === 0

  return shell(
    <>
      <dl className="grid grid-cols-2 gap-3">
        <Stat label="实体" value={stats.term_count} />
        <Stat label="关系" value={stats.edge_count} />
        <Stat label="文档" value={stats.document_count} />
        <Stat label="待审" value={stats.pending_review_count} />
      </dl>

      {isEmpty ? (
        // 四个 0 只说明"这里是空的"，不说明该干什么。这条引导此前由
        // /admin 的落地分流承担（AdminLanding，导航重排时删掉了）——它按
        // 本体确认状态把新租户送去建本体。现在归这里。
        <div className="flex flex-col items-start gap-2">
          <p className="text-sm text-ink-soft">这个领域还没有数据。</p>
          <button
            type="button"
            className={buttonClass}
            disabled={going}
            onClick={() => void goTo(ADMIN_ROUTES.documents)}
          >
            去导入数据
          </button>
        </div>
      ) : (
        stats.pending_review_count > 0 && (
          // 待办为 0 时不给入口：那不是一件等着你做的事，点进去是个空队列。
          <button
            type="button"
            className={buttonClass}
            disabled={going}
            onClick={() => void goTo(ADMIN_ROUTES.reviewRelations)}
          >
            去处理 {stats.pending_review_count} 项待审
          </button>
        )
      )}
    </>,
  )
}

const buttonClass = `min-h-[36px] cursor-pointer self-start rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-ink-soft">{label}</dt>
      {/* 千分位：1204883 读不出来是一百二十万还是十二万。tabular-nums 让
          四个数字的位数对齐，扫一眼就能比大小。 */}
      <dd className="font-mono text-lg font-semibold tabular-nums text-ink">
        {value.toLocaleString()}
      </dd>
    </div>
  )
}
