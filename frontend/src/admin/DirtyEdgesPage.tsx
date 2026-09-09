import { useCallback, useEffect, useState } from 'react'
import { Unlink } from 'lucide-react'
import { Link } from 'react-router-dom'
import { ADMIN_ROUTES, PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`
const linkClass = `font-bold text-ink underline underline-offset-2 hover:text-accent-primary ${focusRing}`

interface DirtyEdge {
  subject_node_key: string
  subject_standard_name: string
  relation_type: string
  object_node_key: string
  object_standard_name: string
  edge_tenant_id: string | null
  subject_tenant_id: string | null
  object_tenant_id: string | null
}

/** 这条边到底哪里不对——说清楚，不然运维不知道该不该删。 */
function whatIsWrong(edge: DirtyEdge, tenantId: string): string {
  if (edge.edge_tenant_id === null) return '这条边没有租户标记'
  if (edge.edge_tenant_id !== tenantId) return `这条边标着别的租户（${edge.edge_tenant_id}）`
  if (edge.object_tenant_id === null) return '对端实体没有租户标记'
  if (edge.object_tenant_id !== tenantId)
    return `对端实体属于别的租户（${edge.object_tenant_id}）`
  return '两端与边的租户标记对不上'
}

/**
 * 脏边与孤儿数据。
 *
 * 脏边的列举此前只在实体详情页里——你得**先知道是哪个实体**才看得到它的
 * 脏边，而脏边的特点恰恰是没人知道它们在哪。运维只能一个实体一个实体点
 * 过去。这一页做的就是那个"发现"。
 *
 * **删除仍然在实体详情页做**，这里只给入口。删除动作（含影响面预览和批量
 * 确认）已经在那边完整实现了，在这里再写一份的话，两份会在"删之前要给用户
 * 看什么"上分叉——而那正是删除最不能出错的地方。
 */
export function DirtyEdgesPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [edges, setEdges] = useState<DirtyEdge[] | null>(null)
  const [truncated, setTruncated] = useState(false)
  const [limit, setLimit] = useState(0)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    document.title = `${PAGE_TITLES.reviewDirtyEdges} · 管理后台`
  }, [])

  const load = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/dirty-edges`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '脏边清单加载失败'))
      }
      const body = (await response.json()) as {
        edges: DirtyEdge[]
        truncated: boolean
        limit: number
      }
      setEdges(body.edges)
      setTruncated(body.truncated)
      setLimit(body.limit)
    } catch (err) {
      // 不退回空数组：「一条脏边都没有」是这一页最不该说错的一句话——
      // 运维会据此认为数据是干净的。
      setError(err instanceof Error ? err.message : '脏边清单加载失败')
    }
  }, [sessionToken, tenantId])

  useEffect(() => {
    void load()
  }, [load])

  const header = (
    <div className="flex flex-col gap-1">
      <h1 className="font-mono text-xl font-semibold text-ink">
        {PAGE_TITLES.reviewDirtyEdges}
      </h1>
      <p className="text-sm text-ink-soft">
        租户标记不对的关系边。它们在实体详情页里看得到，但你得先知道是哪个实体
        ——这一页把整个租户的都列出来。
      </p>
    </div>
  )

  if (error !== null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <div
          role="status"
          className="flex flex-col items-start gap-3 rounded-card border border-subtle bg-card p-4"
        >
          <p className="text-sm text-status-error-strong">{error}</p>
          <button type="button" className={buttonClass} onClick={() => void load()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (edges === null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <Skeleton variant="table-rows" count={5} />
      </div>
    )
  }

  if (edges.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <EmptyState
          icon={Unlink}
          title="没有脏边"
          action="这个租户的每条关系边，两端和边自己的租户标记都是一致的。"
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {header}
      {truncated && (
        // 默默少列的话，运维会以为脏边只有这么多——他清完就以为干净了。
        <p role="status" className="rounded-card border border-subtle bg-card p-3 text-sm text-ink">
          只列了前 {limit} 条，还有更多没显示。清掉一批之后再回来看剩下的。
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {edges.map((edge) => (
          <li
            key={`${edge.subject_node_key}|${edge.relation_type}|${edge.object_node_key}`}
            data-testid="dirty-edge"
            className="flex flex-col gap-1 rounded-card border border-subtle bg-card p-3"
          >
            <p className="font-mono text-sm text-ink">
              {edge.subject_standard_name} —[{edge.relation_type}]→{' '}
              {edge.object_standard_name}
            </p>
            <p className="text-xs text-status-error-strong">
              {whatIsWrong(edge, tenantId)}
            </p>
            {/* 删除在实体详情页做：那边已经有影响面预览和批量确认。在这里
                再写一份的话，两份会在"删之前要给用户看什么"上分叉。 */}
            <Link
              to={`${ADMIN_ROUTES.terms}/${encodeURIComponent(edge.subject_node_key)}`}
              className={`${linkClass} text-xs`}
            >
              去 {edge.subject_standard_name} 的详情页处理
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
