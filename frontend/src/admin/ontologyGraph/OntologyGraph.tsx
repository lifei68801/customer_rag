import { useEffect, useMemo, useRef, useState } from 'react'
import Sigma from 'sigma'
import { buildScene, type ConstraintTriple, type FanoutEntry } from './buildScene'
import { readGraphTheme } from './graphTheme'
import {
  clickSelectionBehavior,
  fitViewBehavior,
  hoverHighlightBehavior,
  type Dispose,
} from './behaviors'

interface Props {
  termTypes: string[]
  constraints: ConstraintTriple[]
  /** 来自 /constraint-fanout。拉取失败时传空数组：没有红边好过标错红边。 */
  fanout: FanoutEntry[]
}

type Selection = { kind: 'node' | 'edge'; id: string } | null

/**
 * 本体图：把「实体类型 + 允许的关系组合」渲染成一张有向图。
 *
 * 为什么值得有这个视图：约束本质上就是 (主语类型, 关系, 宾语类型) 的边表，
 * 用表格展示一张图，等于让人在脑子里做渲染。类型少、边少的规模下，一张图
 * 能一眼看完本体的全貌，而表格要在三个 tab 之间来回翻。
 *
 * 渲染用 sigma + graphology：graphology 把图**数据结构**和渲染分开了，
 * 所以扇出判定这类图算法可以直接在数据层做（见 buildScene），不必依赖
 * 渲染器。
 */
export function OntologyGraph({ termTypes, constraints, fanout }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [selection, setSelection] = useState<Selection>(null)

  // 主题在渲染时解析一次即可——换肤会重新挂载整个页面。
  const theme = useMemo(() => readGraphTheme(), [])
  const { graph, riskyEdges, fanoutByEdge } = useMemo(
    () => buildScene(termTypes, constraints, fanout, theme),
    [termTypes, constraints, fanout, theme],
  )

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const sigma = new Sigma(graph, container, {
      renderEdgeLabels: true,
      defaultEdgeType: 'arrow',
      labelColor: { color: theme.nodeLabel },
      edgeLabelColor: { color: theme.edge },
      labelSize: 12,
      edgeLabelSize: 11,
    })

    const disposers: Dispose[] = [
      hoverHighlightBehavior(sigma, graph, theme),
      clickSelectionBehavior(sigma, setSelection),
      fitViewBehavior(sigma, container),
    ]

    return () => {
      disposers.forEach((dispose) => dispose())
      sigma.kill()
    }
  }, [graph, theme])

  const isolated = termTypes.filter(
    (t) =>
      !constraints.some((c) => c.subject_term_type === t || c.object_term_type === t),
  )

  if (termTypes.length === 0) {
    return (
      <p className="rounded-card border border-subtle bg-card p-6 text-sm text-ink-soft">
        还没有实体类型，本体图无内容可画。先到「实体类型」tab 建几个类型。
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div
        ref={containerRef}
        className="h-[28rem] w-full rounded-card border border-subtle bg-card"
        role="img"
        aria-label={`本体图：${termTypes.length} 个实体类型，${constraints.length} 条允许的关系组合`}
      />

      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-ink-soft">
        <span className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className="inline-block h-0.5 w-6"
            style={{ backgroundColor: theme.edge }}
          />
          {riskyEdges.size > 0 ? '一对一关系' : '关系'}
        </span>
        {riskyEdges.size > 0 && (
          <span className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-1 w-6"
              style={{ backgroundColor: theme.edgeRisky }}
            />
              一对多（沿这一跳计数会放大归属，见扇形陷阱）
          </span>
        )}
        <span>悬停某个类型看它的邻域，点击看详情</span>
      </div>

      {riskyEdges.size > 0 && (
        <p className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
          有 {riskyEdges.size} 条一对多的关系边（红色加粗）：实际数据里一个主语节点沿
          这条关系连到了多个宾语节点。沿这样一跳做计数聚合会把归属放大——需要计数时
          应当用直连边，不要靠多跳推导。
        </p>
      )}

      {isolated.length > 0 && (
        <p className="rounded-card border border-subtle bg-card px-3 py-2 text-sm text-ink-soft">
          孤立类型（没有任何允许的关系组合）：{isolated.join('、')}
        </p>
      )}

      {selection && (
        <SelectionDetail
          selection={selection}
          constraints={constraints}
          fanoutByEdge={fanoutByEdge}
        />
      )}
    </div>
  )
}

function SelectionDetail({
  selection,
  constraints,
  fanoutByEdge,
}: {
  selection: NonNullable<Selection>
  constraints: ConstraintTriple[]
  fanoutByEdge: Map<string, number | null>
}) {
  if (selection.kind === 'node') {
    const outgoing = constraints.filter((c) => c.subject_term_type === selection.id)
    const incoming = constraints.filter((c) => c.object_term_type === selection.id)
    return (
      <div className="rounded-card border border-subtle bg-card p-3 text-sm">
        <p className="font-bold text-ink">{selection.id}</p>
        <p className="mt-1 text-ink-soft">
          作为主语 {outgoing.length} 条，作为宾语 {incoming.length} 条
        </p>
        <ul className="mt-2 space-y-1 font-mono text-xs text-ink-soft">
          {outgoing.map((c) => (
            <li key={`out-${c.relation_type}-${c.object_term_type}`}>
              → {c.relation_type} → {c.object_term_type}
            </li>
          ))}
          {incoming.map((c) => (
            <li key={`in-${c.subject_term_type}-${c.relation_type}`}>
              ← {c.relation_type} ← {c.subject_term_type}
            </li>
          ))}
        </ul>
      </div>
    )
  }

  const [subject, relation, object] = selection.id.split('|')
  const fanout = fanoutByEdge.get(selection.id)
  return (
    <div className="rounded-card border border-subtle bg-card p-3 text-sm">
      <p className="font-mono text-ink">
        {subject} → {relation} → {object}
      </p>
      <p className="mt-1 text-ink-soft">
        {fanout === undefined || fanout === null
          ? '扇出：未知（探测失败或该类型还没有数据）'
          : fanout > 1
            ? `扇出 ${fanout}：一个${subject}最多连到 ${fanout} 个不同的${object}，沿这一跳计数会放大归属`
            : `扇出 ${fanout}：函数关系，可以安全地沿这一跳计数`}
      </p>
    </div>
  )
}
