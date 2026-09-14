import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Unlink } from 'lucide-react'
import { ADMIN_ROUTES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface StructureRisk {
  subject_term_type: string
  relation_type: string
  object_term_type: string
  fanout: number
}

interface GraphStructure {
  entity_types: { term_type: string; count: number }[]
  relation_types: { relation_type: string; edge_count: number }[]
  risks: StructureRisk[]
  graph_term_count: number
  connected_term_count: number
}

/** 构成条最多分这么多段，其余并成「其他」。段太多就读不出主次了。 */
const MAX_SLICES = 5
/** 关系条最多列这么多行。 */
const MAX_RELATION_ROWS = 4
/** 风险最多直接展开这么多条，其余折成一句。 */
const MAX_RISK_ROWS = 2

/**
 * 一个领域的图谱**形状**：由什么构成、靠什么连起来、哪里的结构会骗人。
 *
 * ## 为什么看板需要它
 *
 * 卡上原本是六个规模数字。「实体 7,826」说不出这些实体是什么，「关系 31k」
 * 里 10,000 条订单→产品和 30 条产品→公司被抹成了同一个数。而建模出问题时，
 * 症状恰恰出现在构成上：一列自由文本被建成实体，规模数字只多了 600，构成条
 * 上却是突然冒出来的一大块。
 *
 * ## 三件事按危险程度排
 *
 * 1. **扇出风险**排最前。一个主语沿某条关系连到多个宾语时，沿它做计数聚合
 *    会把归属放大——demo 上"某公司有多少订单"恒等于订单总数就是这么来的。
 *    这件事本体层看不出来，只有真实数据能判定，而它此前只在「本体图」那一页
 *    才显示：用户得先点进去才可能发现，通常是先问错一次才发现。
 * 2. **孤立实体**：建出来了却一条边都没连上，导入报告一切正常，查询什么都
 *    查不到。
 * 3. **构成**：没有风险时它就是这张卡的主体，回答"这个知识库装的是什么"。
 *
 * ## 取色
 *
 * 构成条用同一个强调色的透明度梯度，不用五种不同的颜色：一是换肤时不会跟
 * 页面其余部分脱节（同 ontologyGraph/graphTheme 的取舍），二是颜色在这里
 * 不承担表意——每一段的名字和条数都在图例里写着，色块只是把它们跟条子对上。
 */
export function DomainStructure({
  tenantId,
  onOpen,
  disabled,
}: {
  tenantId: string
  /** 去这个领域的某一页（由卡片提供：要先切当前租户再跳）。 */
  onOpen: (path: string) => void
  disabled: boolean
}) {
  const { sessionToken } = useAdminAuth()
  const [structure, setStructure] = useState<GraphStructure | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!sessionToken) return
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/dashboard/structure`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '图谱结构没算出来'))
      }
      setStructure((await response.json()) as GraphStructure)
    } catch (err) {
      setError(err instanceof Error ? err.message : '图谱结构没算出来')
    }
  }, [sessionToken, tenantId])

  useEffect(() => {
    void load()
  }, [load])

  if (error !== null) {
    // 结构算不出来只让这一块降级，不让整张卡变成错误页：规模数字来自另一个
    // 端点，它们是好的，凭什么一起看不到。
    return (
      <p className="flex flex-wrap items-center gap-2 text-xs text-ink-soft">
        <span>{error}</span>
        <button type="button" onClick={() => void load()} className={`font-bold underline ${focusRing}`}>
          重试
        </button>
      </p>
    )
  }

  if (structure === null) {
    return (
      <div
        data-testid={`domain-structure-${tenantId}-skeleton`}
        aria-hidden="true"
        className="flex flex-col gap-2"
      >
        <div className="h-3 animate-pulse rounded-chip bg-interactive-hover motion-reduce:animate-none" />
        <div className="h-3 w-2/3 animate-pulse rounded-chip bg-interactive-hover motion-reduce:animate-none" />
      </div>
    )
  }

  const isolated = Math.max(0, structure.graph_term_count - structure.connected_term_count)

  return (
    <div data-testid={`domain-structure-${tenantId}`} className="flex flex-col gap-3">
      <RiskList risks={structure.risks} onOpen={onOpen} disabled={disabled} />
      {isolated > 0 && (
        <p className="flex items-start gap-1.5 text-xs text-ink">
          <Unlink aria-hidden="true" className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-ink-soft" />
          <span>
            <span className="font-mono font-bold tabular-nums">{isolated.toLocaleString()}</span> 个实体
            没有连上任何关系（{Math.round((isolated / structure.graph_term_count) * 100)}%），查询问不到它们。
          </span>
        </p>
      )}
      <CompositionBar entityTypes={structure.entity_types} />
      <RelationBars relationTypes={structure.relation_types} />
    </div>
  )
}

function RiskList({
  risks,
  onOpen,
  disabled,
}: {
  risks: StructureRisk[]
  onOpen: (path: string) => void
  disabled: boolean
}) {
  if (risks.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5 rounded-card border border-status-error bg-paper p-2.5">
      {risks.slice(0, MAX_RISK_ROWS).map((risk) => (
        <p
          key={`${risk.subject_term_type}|${risk.relation_type}|${risk.object_term_type}`}
          className="flex items-start gap-1.5 text-xs text-ink"
        >
          <AlertTriangle
            aria-hidden="true"
            className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-status-error-strong"
          />
          <span>
            <span className="font-mono">
              {risk.subject_term_type} —{risk.relation_type}→ {risk.object_term_type}
            </span>{' '}
            是一对多（一个{risk.subject_term_type}连到 {risk.fanout} 个
            {risk.object_term_type}）：沿这条边做计数会把归属放大。
          </span>
        </p>
      ))}
      {risks.length > MAX_RISK_ROWS && (
        <p className="text-xs text-ink-soft">还有 {risks.length - MAX_RISK_ROWS} 条同类的边。</p>
      )}
      <button
        type="button"
        disabled={disabled}
        onClick={() => onOpen(ADMIN_ROUTES.ontologyGraph)}
        className={`min-h-[36px] cursor-pointer self-start rounded-control border border-subtle bg-card px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
      >
        去本体图看这几条边
      </button>
    </div>
  )
}

function CompositionBar({ entityTypes }: { entityTypes: { term_type: string; count: number }[] }) {
  const total = entityTypes.reduce((sum, t) => sum + t.count, 0)
  if (total === 0) return null
  const head = entityTypes.slice(0, MAX_SLICES)
  const restCount = entityTypes.slice(MAX_SLICES).reduce((sum, t) => sum + t.count, 0)
  const slices = restCount > 0 ? [...head, { term_type: '其他', count: restCount }] : head
  const summary = slices.map((s) => `${s.term_type} ${s.count}`).join('，')

  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-xs font-bold text-ink-soft">
        实体构成 · 共 <span className="font-mono tabular-nums">{total.toLocaleString()}</span>
      </p>
      {/* 条子本身对读屏软件是一张图：把它读成一句话，不让人去逐段猜。 */}
      <div
        role="img"
        aria-label={`实体构成：${summary}`}
        className="flex h-2.5 w-full overflow-hidden rounded-chip bg-interactive-hover"
      >
        {slices.map((slice, index) => (
          <div
            key={slice.term_type}
            style={{
              width: `${(slice.count / total) * 100}%`,
              // 透明度梯度而不是五种颜色：颜色在这里不表意，图例已经写了名字
              // 和条数，色块只负责把两者对上。最小 0.35，再淡就贴不住背景了。
              opacity: Math.max(0.35, 1 - index * 0.16),
            }}
            className="bg-accent-primary"
          />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-soft">
        {slices.map((slice, index) => (
          <li key={slice.term_type} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              style={{ opacity: Math.max(0.35, 1 - index * 0.16) }}
              className="inline-block h-2 w-2 flex-shrink-0 rounded-full bg-accent-primary"
            />
            <span className="text-ink">{slice.term_type}</span>
            <span className="font-mono tabular-nums">{slice.count.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function RelationBars({
  relationTypes,
}: {
  relationTypes: { relation_type: string; edge_count: number }[]
}) {
  if (relationTypes.length === 0) return null
  const max = relationTypes[0].edge_count || 1
  const rows = relationTypes.slice(0, MAX_RELATION_ROWS)

  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-bold text-ink-soft">关系构成</p>
      {rows.map((row) => (
        <div key={row.relation_type} className="flex items-center gap-2 text-xs">
          <span className="w-[42%] flex-shrink-0 truncate font-mono text-ink" title={row.relation_type}>
            {row.relation_type}
          </span>
          <span className="h-2 flex-1 rounded-chip bg-interactive-hover">
            {/* 长度是唯一的量度：30 条和 10,000 条的差别要看得出来，而不是
                两条一样长的彩条配两个数字。最小 2% 让非零的那些不至于消失。 */}
            <span
              style={{ width: `${Math.max(2, (row.edge_count / max) * 100)}%` }}
              className="block h-full rounded-chip bg-accent-primary opacity-70"
            />
          </span>
          <span className="w-16 flex-shrink-0 text-right font-mono tabular-nums text-ink">
            {row.edge_count.toLocaleString()}
          </span>
        </div>
      ))}
      {relationTypes.length > MAX_RELATION_ROWS && (
        <p className="text-xs text-ink-soft">
          还有 {relationTypes.length - MAX_RELATION_ROWS} 种关系
        </p>
      )}
    </div>
  )
}
