import { Suspense, lazy, useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Share2 } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import { GRAPH_PREVIEW_QUERY_KEY, PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { GraphErrorBoundary } from './dataGraph/GraphErrorBoundary'
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
  // 最后一次发出的请求序号。响应回来时不是它就丢弃——搜 A（邻域大、三秒）
  // 之后改搜 B（很快回来），A 的响应后到会无条件盖掉 B：搜索框和地址栏写着
  // B，图和截断提示却是 A 的，而且这个错误不会自我暴露。
  const latestRequestRef = useRef(0)
  // 已经取过数的 node_key。handleSubmit 直接取数，这里用来让监听 URL 的
  // effect 不要为同一个 key 再发一次。
  const loadedKeyRef = useRef<string | null>(null)
  const [data, setData] = useState<Neighborhood | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    document.title = `${PAGE_TITLES.dataGraph} · 管理后台`
  }, [])

  const load = useCallback(
    async (nodeKey: string) => {
      if (!sessionToken || !tenantId || !nodeKey) return
      const requestId = latestRequestRef.current + 1
      latestRequestRef.current = requestId
      loadedKeyRef.current = nodeKey
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
        const payload = (await response.json()) as Neighborhood
        if (latestRequestRef.current !== requestId) return
        setData(payload)
      } catch (err) {
        if (latestRequestRef.current !== requestId) return
        // 不画空图：空图看起来像「这个实体一条关系都没有」，而真相可能是
        // 它根本不存在——两句话要用户做的事完全不同（改搜索词 vs 去建它）。
        setData(null)
        setError(err instanceof Error ? err.message : '邻域图没查出来')
      } finally {
        // 过期的那次不许关掉 loading：最新那次还在飞，关掉的话骨架屏消失、
        // 页面看起来像已经加载完了。
        if (latestRequestRef.current === requestId) setLoading(false)
      }
    },
    [sessionToken, tenantId],
  )

  // 带着 ?node_key= 进来时直接画。这一页最自然的入口不是搜索框，是「我正在
  // 看这个实体，想看看它连着什么」——到了还要再输一遍名字的话那个入口就
  // 白给了。
  useEffect(() => {
    if (!fromUrl || loadedKeyRef.current === fromUrl) return
    setQuery(fromUrl)
    void load(fromUrl)
  }, [fromUrl, load])

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    // 这里直接取数，effect 靠 loadedKeyRef 认出"这个 key 已经取过了"而
    // 不再重复发一次。
    //
    // 不能改成只写 URL 让 effect 去取：**同一个 key 重搜时 URL 根本没变**，
    // effect 不会触发。而后端 503 的文案就是"请稍后重试"——原样再点一次
    // 「画出来」什么也不发生的话，它指的那个纠正动作在界面上做不到。
    //
    // 仍然写 URL：这一页的状态就是"在看哪个实体"，放进地址栏之后它可分享、
    // 可刷新、可后退。
    setSearchParams(query ? { [GRAPH_PREVIEW_QUERY_KEY]: query } : {})
    void load(query)
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
        {/* 取数途中**不**禁用：大邻域要好几秒，这几秒里不让人改搜别的
            实体是很难受的。并发靠 latestRequestRef 挡住（只有最后发出的
            那次允许写 state），不靠禁用按钮回避。 */}
        <button type="submit" className={buttonClass} disabled={!query}>
          画出来
        </button>
      </form>

      {error !== null && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-3 rounded-card border border-subtle bg-card p-3 text-sm text-status-error-strong"
        >
          <span>{error}</span>
          {/* 后端 503 的文案写着"请稍后重试"。重试得在界面上真能做到，
              否则那句话只是描述了一个用户执行不了的动作。 */}
          <button type="button" className={buttonClass} onClick={() => void load(query)}>
            重试
          </button>
        </div>
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
          {/* 边界只包渲染层：WebGL 不可用时画不出来，但上面那条截断提示、
              和外面的搜索框都还在——用户看得见发生了什么，也还能换一个
              实体接着看。 */}
          <GraphErrorBoundary
            fallback={(retry) => (
              <div
                role="alert"
                className="flex flex-col gap-2 rounded-card border border-subtle bg-card p-4 text-sm text-ink"
              >
                <span>
                  {`这张图没画出来（浏览器可能不支持 WebGL）。数据已经取到了：共 ${data.total_nodes.toLocaleString()} 个节点。`}
                </span>
                <div>
                  <button type="button" className={buttonClass} onClick={retry}>
                    再画一次
                  </button>
                </div>
              </div>
            )}
          >
            <Suspense fallback={<Skeleton variant="card-list" count={1} />}>
              <NeighborhoodGraph center={data.center} nodes={data.nodes} edges={data.edges} />
            </Suspense>
          </GraphErrorBoundary>
        </div>
      )}
    </div>
  )
}
