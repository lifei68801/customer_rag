import Graph from 'graphology'
import forceAtlas2 from 'graphology-layout-forceatlas2'
import { EDGE_SIZE, EDGE_SIZE_RISKY, nodeSize, type GraphTheme } from './graphTheme'

/** 约束就是一条边：主语类型 --关系--> 宾语类型。跟后端 Constraint 同形。 */
export interface ConstraintTriple {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface BuildResult {
  graph: Graph
  /** 真实数据里扇出 > 1 的边 key，供图例和详情面板复用。 */
  riskyEdges: Set<string>
  /** edgeKey -> 扇出度，详情面板要显示具体数字。 */
  fanoutByEdge: Map<string, number | null>
}

/**
 * 真实扇出：来自后端 /constraint-fanout，按实际图谱数据算出「一个主语节点
 * 沿这条关系最多连到几个不同的宾语节点」。
 *
 * 为什么不用本体层的静态判定：本体只声明「产品 SOLD_BY 公司」这一条边，
 * 看不出它是不是一对多。demo 的真实扇形陷阱正是这种——本体层完全正常，
 * 数据层是 10 个产品 × 3 家公司的全交叉。我先做过一版按「同一主语类型 +
 * 同一关系指向多个宾语类型」判定的静态版本，在 demo 上一条都不触发。
 *
 * fanout > 1 表示这一跳不是函数关系，沿它做计数聚合会把归属放大。
 * null 表示探测失败（图谱不可用、该类型还没有节点），按未知处理、不标红——
 * 把「查不到」显示成「有风险」是另一种撒谎。
 */
export interface FanoutEntry {
  subject_term_type: string
  relation_type: string
  object_term_type: string
  fanout: number | null
}

function riskyFromFanout(fanout: FanoutEntry[]): Set<string> {
  const risky = new Set<string>()
  for (const f of fanout) {
    if (f.fanout !== null && f.fanout > 1) risky.add(edgeKey(f))
  }
  return risky
}

export function edgeKey(c: ConstraintTriple): string {
  return `${c.subject_term_type}|${c.relation_type}|${c.object_term_type}`
}

/**
 * 把约束三元组和实体类型列表构建成可渲染的图。
 *
 * termTypes 单独传进来、而不是从约束里推断：没有任何约束引用的孤立类型
 * 也必须出现在图上——"这个类型还没接进本体"正是用户最需要看到的信号，
 * 从边表推节点会让它静默消失。
 */
export function buildScene(
  termTypes: string[],
  constraints: ConstraintTriple[],
  fanout: FanoutEntry[],
  entityCounts: Record<string, number>,
  theme: GraphTheme,
): BuildResult {
  const graph = new Graph({ multi: true, type: 'directed' })
  const riskyEdges = riskyFromFanout(fanout)

  const degree = new Map<string, number>()
  for (const c of constraints) {
    degree.set(c.subject_term_type, (degree.get(c.subject_term_type) ?? 0) + 1)
    degree.set(c.object_term_type, (degree.get(c.object_term_type) ?? 0) + 1)
  }

  const allNodes = new Set<string>(termTypes)
  for (const c of constraints) {
    allNodes.add(c.subject_term_type)
    allNodes.add(c.object_term_type)
  }

  // 初始坐标撒在一个圆上而不是随机：forceAtlas2 从对称初值出发收敛得更稳，
  // 每次打开图的布局也更接近，不会让人以为数据变了。
  const nodes = [...allNodes]
  nodes.forEach((value, index) => {
    const angle = (2 * Math.PI * index) / Math.max(nodes.length, 1)
    const count = entityCounts[value]
    graph.addNode(value, {
      // 标签带上实体数：图从"本体长什么样"变成"本体现在装了多少数据"。
      // 没有计数（该类型一条实体都没有）时不显示 "(0)"——那会跟"还没同步
      // 过计数"混淆，而空标签本身已经说明问题，孤立类型另有单独提示。
      label: count === undefined ? value : `${value} (${count})`,
      entityCount: count ?? 0,
      x: Math.cos(angle),
      y: Math.sin(angle),
      size: nodeSize(degree.get(value) ?? 0),
      color: theme.node,
      labelColor: theme.nodeLabel,
      isolated: (degree.get(value) ?? 0) === 0,
    })
  })

  for (const c of constraints) {
    const key = edgeKey(c)
    const risky = riskyEdges.has(key)
    graph.addEdgeWithKey(key, c.subject_term_type, c.object_term_type, {
      label: c.relation_type,
      relationType: c.relation_type,
      size: risky ? EDGE_SIZE_RISKY : EDGE_SIZE,
      color: risky ? theme.edgeRisky : theme.edge,
      risky,
      type: 'arrow',
    })
  }

  // 只有边数足够时才跑力导向：一两条边时它会把节点甩得很远，圆形初值反而更好看。
  if (graph.size > 1) {
    forceAtlas2.assign(graph, {
      iterations: 120,
      settings: { ...forceAtlas2.inferSettings(graph), scalingRatio: 12, gravity: 1.4 },
    })
  }

  const fanoutByEdge = new Map<string, number | null>(
    fanout.map((f) => [edgeKey(f), f.fanout]),
  )
  return { graph, riskyEdges, fanoutByEdge }
}
