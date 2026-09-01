import { Suspense, lazy, useState } from 'react'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useOntologyData } from './useOntologyData'
import { useOntologyVersion } from './useOntologyVersion'

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
  const [error, setError] = useState<string | null>(null)
  const [view] = useOntologyVersion()

  const { termTypes, constraints, fanout, entityCounts, loaded } = useOntologyData({
    sessionToken,
    tenantId,
    view,
    withGraphOverlay: true,
    onError: setError,
  })

  return (
    <div data-testid="ontology-graph-page" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">本体图</h1>
        <p className="text-sm text-ink-soft">
          实体类型之间允许存在哪些关系。红边表示图谱里实际数据的扇出超过一对多，做路径统计时会重复计数。
        </p>
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
