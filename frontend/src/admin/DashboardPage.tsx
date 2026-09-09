import { useCallback, useEffect, useState } from 'react'
import { Boxes } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { useAdminAuth } from './useAdminAuth'
import { DomainCard, type Domain } from './DomainCard'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/** 没挂组织的那些卡片归在这一组下。org_id 为 null 是合法状态，不是错误。 */
const NO_ORG = 'none'

/**
 * 看板：登录后的落地页，一屏列出这个账号能访问的所有领域。
 *
 * **这一页只负责拉清单和分组，不替卡片取数**（spec D5 裁决二）。每张卡
 * 自己请求自己的统计、自己落位——这里统一 `Promise.all` 再一次性渲染的话，
 * 第一张卡也要等最慢的那个领域算完，而每个领域的边计数都是一次图查询。
 *
 * 它是唯一的**组织级**页面：不需要"当前租户"（见 adminRoutes.ts 的
 * NON_TENANT_ROUTE_KEYS）。新登录的 admin（tenant_id 恒为 None）第一眼看到
 * 的必须是他的领域，不是「请先选择一个租户」。
 */
export function DashboardPage() {
  const { sessionToken } = useAdminAuth()
  const [domains, setDomains] = useState<Domain[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.title = `${PAGE_TITLES.dashboard} · 管理后台`
  }, [])

  const load = useCallback(async () => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch('/api/admin/dashboard/domains', sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '领域清单加载失败'))
      }
      setDomains(((await response.json()) as { domains: Domain[] }).domains)
    } catch (err) {
      // 不能退回空数组：「你没有任何领域」和「清单没拉回来」在界面上长得
      // 一样，而前者会让用户去找管理员要授权，后者该报修。
      setError(err instanceof Error ? err.message : '领域清单加载失败')
    }
  }, [sessionToken])

  useEffect(() => {
    void load()
  }, [load])

  const header = (
    <div className="flex flex-col gap-1">
      <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.dashboard}</h1>
      <p className="text-sm text-ink-soft">
        你能访问的每个领域一张卡。数字都是实时算的，所以慢的那几张会晚一点落位。
      </p>
    </div>
  )

  if (error !== null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <div role="status" className="flex flex-col items-start gap-3 rounded-card border border-subtle bg-card p-4">
          <p className="text-sm text-status-error-strong">{error}</p>
          <button
            type="button"
            className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
            onClick={() => void load()}
          >
            重试
          </button>
        </div>
      </div>
    )
  }

  if (domains !== null && domains.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <EmptyState
          icon={Boxes}
          title="还没有任何领域"
          action={
            <>
              一个领域就是一个知识库（一套本体 + 一张图）。管理员可以在
              <Link
                to={ADMIN_ROUTES.tenants}
                className={`mx-1 font-bold underline underline-offset-2 ${focusRing}`}
              >
                租户管理
              </Link>
              里新建一个；如果你不是管理员，请找管理员给你的账号授权。
            </>
          }
        />
      </div>
    )
  }

  // 分组时保留清单本身的顺序（后端按 tenant_id 排过），并让没挂组织的那组
  // 排在最后：它是"其余"，摆在前面会读成一个正经的组织。
  const groups = new Map<string, { label: string; domains: Domain[] }>()
  for (const domain of domains ?? []) {
    const key = domain.org_id ?? NO_ORG
    if (!groups.has(key)) {
      groups.set(key, { label: domain.org_name ?? '未归属组织', domains: [] })
    }
    groups.get(key)!.domains.push(domain)
  }
  const ordered = [...groups.entries()].sort(([a], [b]) =>
    a === NO_ORG ? 1 : b === NO_ORG ? -1 : 0,
  )

  return (
    <div className="flex flex-col gap-6">
      {header}
      {ordered.map(([key, group]) => (
        <section key={key} data-testid={`org-group-${key}`} className="flex flex-col gap-3">
          <h2 className="font-mono text-xs font-bold uppercase tracking-wide text-ink-soft">
            {group.label}
          </h2>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.domains.map((domain) => (
              <DomainCard key={domain.tenant_id} domain={domain} />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}
