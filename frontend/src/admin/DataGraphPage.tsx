import { Suspense, lazy, useCallback, useEffect, useState, type FormEvent } from 'react'
import { Share2 } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import type { GraphEdge, GraphNode } from './dataGraph/NeighborhoodGraph'

// 图不在主包里：sigma + graphology 有几百 kB，而大部分会话根本不打开它。
// 写法照抄 OntologyGraphPage.tsx。
const NeighborhoodGraph = lazy(() =>
  import('./dataGraph/NeighborhoodGraph').then((m) => ({ default: m.NeighborhoodGraph })),
)

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`

/** URL 上带这个参数时直接画，不用再搜一遍。实体明细页的「看图」入口用它。 */
export const GRAPH_PREVIEW_QUERY_KEY = 'node_key'

interface Neighborhood {
  center: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  truncated: boolean
  total_nodes: number
  shown_nodes: number
}

/**
 * 图谱预览：以一个实体为中心画它的邻域（spec D6）。
 *
 * **取数、截断提示、错误都在这一层**，画图在懒加载的 `NeighborhoodGraph` 里。
 * 这个切分不是为了好测：截断提示属于数据层——WebGL 不可用、图根本画不出来
 * 时，「只画了 300 个、其实有 1013 个」这句话也必须出现。
 */
export function DataGraphPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [searchParams, setSearchParams] = useSearchParams()
  const fromUrl = searchParams.get(GRAPH_PREVIEW_QUERY_KEY) ?? ''

  const [query, setQuery] = useState(fromUrl)
  const [data, setData] = useState<Neighborhood | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    document.title = `${PAGE_TITLES.dataGraph} · 管理后台`
  }, [])

  const load = useCallback(
    async (nodeKey: string) => {
      if (!sessionToken || !tenantId || !nodeKey) return
      setLoading(true)
      setError(null)
      try {
        const response = await adminFetch(
          `/api/admin/${encodeURIComponent(tenantId)}/graph-preview/${encodeURIComponent(nodeKey)}`,
          sessionToken,
        )
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          throw new Error(extractErrorDetail(body, '邻域图没查出来'))
        }
        setData((await response.json()) as Neighborhood)
      } catch (err) {
        // 不画空图：空图看起来像「这个实体一条关系都没有」，而真相可能是
        // 它根本不存在——两句话要用户做的事完全不同（改搜索词 vs 去建它）。
        setData(null)
        setError(err instanceof Error ? err.message : '邻域图没查出来')
      } finally {
        setLoading(false)
      }
    },
    [sessionToken, tenantId],
  )

  // 带着 ?node_key= 进来时直接画。这一页最自然的入口不是搜索框，是「我正在
  // 看这个实体，想看看它连着什么」——到了还要再输一遍名字的话那个入口就
  // 白给了。
  useEffect(() => {
    if (fromUrl) {
      setQuery(fromUrl)
      void load(fromUrl)
    }
  }, [fromUrl, load])

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    // **只写 URL，不直接 load。** URL 是这一页唯一的事实来源：写进去之后
    // 上面那个 effect 会去取数。两处都调的话每次搜索都发两个请求——一次
    // 来自这里，一次来自 effect 看到 URL 变了。
    //
    // 顺带的好处：这一页的状态就是"在看哪个实体"，放进地址栏之后它可分享、
    // 可刷新、可后退。
    setSearchParams(query ? { [GRAPH_PREVIEW_QUERY_KEY]: query } : {})
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.dataGraph}</h1>
        <p className="text-sm text-ink-soft">
          以一个实体为中心，看它连着什么。输入实体的 node_key（形如 产品:Beer）。
        </p>
      </div>

      {/* 搜索框始终在。出错时把整页换成一条错误的话，用户连重新搜一个都
          做不到。 */}
      <form className="flex flex-wrap items-center gap-2" onSubmit={handleSubmit}>
        <input
          aria-label="实体 node_key"
          className={`${inputClass} min-w-[18rem] flex-1`}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="产品:Beer"
        />
        <button type="submit" className={buttonClass} disabled={loading || !query}>
          画出来
        </button>
      </form>

      {error !== null && (
        <p role="alert" className="rounded-card border border-subtle bg-card p-3 text-sm text-status-error-strong">
          {error}
        </p>
      )}

      {loading && <Skeleton variant="card-list" count={1} />}

      {!loading && error === null && data === null && (
        <EmptyState
          icon={Share2}
          title="还没有画任何东西"
          action="输入一个实体的 node_key 再点「画出来」。从实体明细页点「看图」也会带着那个实体跳过来。"
        />
      )}

      {!loading && data !== null && (
        <div className="flex flex-col gap-2">
          {/* 截断提示。**这是这一页最重要的一句话**：没有它，用户会对着
              一张不完整的图下一个完整的结论——「这个实体只连了 300 个
              东西」，而它连着 1013 个。
              没截断时不显示：恒显示的话用户很快就不看它了，而它在真的
              截断时是关键信息。 */}
          {data.truncated && (
            <p role="status" className="rounded-card border border-subtle bg-card p-3 text-sm text-ink">
              {`这张图只画了 ${data.shown_nodes.toLocaleString()} / 共 ${data.total_nodes.toLocaleString()} 个节点。剩下的没画出来——别拿这张图下「它只连了这些」的结论。`}
            </p>
          )}
          <Suspense fallback={<Skeleton variant="card-list" count={1} />}>
            <NeighborhoodGraph center={data.center} nodes={data.nodes} edges={data.edges} />
          </Suspense>
        </div>
      )}
    </div>
  )
}
