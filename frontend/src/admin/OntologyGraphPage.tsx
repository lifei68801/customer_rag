import { Suspense, lazy, useState } from 'react'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useOntologyData } from './useOntologyData'
import type { ViewMode } from './ontologyTypes'

// 图不在主包里：sigma + graphology 有几百 kB，而大部分会话根本不打开它。
const OntologyGraph = lazy(() =>
  import('./ontologyGraph/OntologyGraph').then((m) => ({ default: m.OntologyGraph })),
)

/**
 * 本体图的独立页面。
 *
 * 此前它是「本体管理 › 约束 › 图」——第三层，而且入口是"表格/图"这个
 * 分段控件，不点开约束 tab 根本不知道有图。它是理解整个本体的入口视图，
 * 不是约束表的一种可选呈现，所以给它自己的地址。
 *
 * 约束页仍然保留图形态：在表上加完一条约束，就地看一眼它落在图的哪里，
 * 比跳到另一个页面顺手。两边取数走同一个 useOntologyData。
 */
export function OntologyGraphPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [view, setView] = useState<ViewMode>('draft')
  const [error, setError] = useState<string | null>(null)

  const { termTypes, constraints, fanout, entityCounts, loaded } = useOntologyData({
    sessionToken,
    tenantId,
    view,
    withGraphOverlay: true,
    onError: setError,
  })

  const segmentClass = (active: boolean) =>
    `min-h-[36px] cursor-pointer px-3 text-xs font-bold uppercase tracking-wide transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
      active ? 'bg-ink text-paper' : 'bg-paper text-ink hover:bg-interactive-hover'
    }`

  return (
    <div data-testid="ontology-graph-page" className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="font-mono text-xl font-semibold text-ink">本体图</h1>
          <p className="text-sm text-ink-soft">
            实体类型之间允许存在哪些关系。红边表示图谱里实际数据的扇出超过一对多，做路径统计时会重复计数。
          </p>
        </div>
        {/* 草稿/已确认跟约束页是同一个轴，样式也保持一致，免得同一个概念在
            两个页面上长得不一样。 */}
        <div
          className="flex overflow-hidden rounded-control border border-subtle"
          role="group"
          aria-label="本体版本"
        >
          <button type="button" className={segmentClass(view === 'draft')} onClick={() => setView('draft')}>
            草稿
          </button>
          <button
            type="button"
            className={segmentClass(view === 'confirmed')}
            onClick={() => setView('confirmed')}
          >
            已确认
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          {error}
        </p>
      )}

      {!loaded && <Skeleton variant="table-rows" count={4} />}
      {loaded && (
        <Suspense fallback={<Skeleton variant="table-rows" count={4} />}>
          <OntologyGraph
            termTypes={termTypes}
            constraints={constraints}
            fanout={fanout}
            entityCounts={entityCounts}
          />
        </Suspense>
      )}
    </div>
  )
}
