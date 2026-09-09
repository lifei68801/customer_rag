import { useEffect, useRef } from 'react'
import Graph from 'graphology'
import forceAtlas2 from 'graphology-layout-forceatlas2'
import Sigma from 'sigma'

export interface GraphNode {
  node_key: string
  standard_name: string
  term_type: string | null
}

export interface GraphEdge {
  source: string
  relation_type: string
  target: string
}

/**
 * 按实体类型给一个稳定的颜色。
 *
 * 稳定是关键：同一个类型在两次打开之间必须是同一个颜色，否则用户没法把
 * 「上次那批蓝点」和这次的对上。所以用类型名的哈希取色，不用遍历顺序。
 */
function colorForType(termType: string | null): string {
  if (!termType) return '#9ca3af'
  let hash = 0
  for (let i = 0; i < termType.length; i += 1) {
    hash = (hash * 31 + termType.charCodeAt(i)) % 360
  }
  return `hsl(${hash}, 62%, 52%)`
}

/**
 * 以一个实体为中心的邻域图。**只负责画**。
 *
 * 取数、截断提示、错误处理都在 `DataGraphPage` 里——那个切分不是为了好测：
 * 截断提示属于数据层，它在 WebGL 不可用、这个组件根本画不出来时也必须出现。
 *
 * 这个文件被 `React.lazy` 懒加载（见 DataGraphPage）：sigma + graphology 有
 * 几百 kB，而大部分会话根本不打开这一页。
 */
export function NeighborhoodGraph({
  center,
  nodes,
  edges,
}: {
  center: string
  nodes: GraphNode[]
  edges: GraphEdge[]
}) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const graph = new Graph({ multi: true })
    for (const node of nodes) {
      graph.addNode(node.node_key, {
        label: node.standard_name,
        // 中心节点画大一圈：一屏三百个点里，用户第一件事是找到他搜的那个。
        size: node.node_key === center ? 14 : 7,
        color: colorForType(node.term_type),
        // 初始位置随机撒开。全落在原点的话 forceAtlas2 第一帧会算出一个
        // 除零，整张图变成一个点。
        x: Math.random(),
        y: Math.random(),
      })
    }
    for (const edge of edges) {
      // 端点缺失时跳过而不是抛异常。后端已经保证了边的两端都在 nodes 里
      // （截断时会一起截），这里是防御——抛出去的话整张图变白，比少画
      // 一条边糟得多。
      if (!graph.hasNode(edge.source) || !graph.hasNode(edge.target)) continue
      graph.addEdge(edge.source, edge.target, { label: edge.relation_type, size: 1 })
    }

    forceAtlas2.assign(graph, { iterations: 120, settings: { gravity: 1, scalingRatio: 12 } })
    const renderer = new Sigma(graph, container, { renderEdgeLabels: true })
    return () => renderer.kill()
  }, [center, nodes, edges])

  return (
    <div
      ref={containerRef}
      data-testid="neighborhood-graph"
      className="h-[520px] w-full rounded-card border border-subtle bg-card"
    />
  )
}
