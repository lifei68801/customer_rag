import type Sigma from 'sigma'
import type Graph from 'graphology'
import type { GraphTheme } from './graphTheme'

/**
 * 图交互按行为拆分——每个行为一个独立函数，各自订阅自己需要的事件、
 * 各自返回自己的清理函数。
 *
 * 这个组织方式借鉴自 semantica 的 explorer（MIT，
 * https://github.com/semantica-agi/semantica，见 GraphWorkspace/behaviors/）：
 * 图交互天然容易堆成一个几千行的巨型组件，拆开之后每个行为可以单独
 * 读、单独换、单独去掉。这里只实现本体图真正需要的三个，没有照搬它那套
 * 为 11 个工作区服务的完整实现。
 *
 * 约定：每个行为接收 sigma 实例和它需要的状态，返回一个 dispose 函数。
 * 行为之间不互相引用，只通过传入的回调对外通信。
 */

export type Dispose = () => void

/** 悬停某个节点时，把与它无关的节点和边压暗，突出它的一跳邻域。 */
export function hoverHighlightBehavior(
  sigma: Sigma,
  graph: Graph,
  theme: GraphTheme,
): Dispose {
  let hovered: string | null = null

  const relatedTo = (node: string): Set<string> => {
    const set = new Set<string>([node])
    graph.forEachNeighbor(node, (neighbor) => set.add(neighbor))
    return set
  }

  sigma.setSetting('nodeReducer', (node, data) => {
    if (!hovered) return data
    return relatedTo(hovered).has(node)
      ? data
      : { ...data, color: theme.dimmed, label: '' }
  })

  sigma.setSetting('edgeReducer', (edge, data) => {
    if (!hovered) return data
    const [source, target] = graph.extremities(edge)
    return source === hovered || target === hovered
      ? { ...data, color: theme.highlight }
      : { ...data, color: theme.dimmed, label: '' }
  })

  const onEnter = ({ node }: { node: string }) => {
    hovered = node
    sigma.refresh()
  }
  const onLeave = () => {
    hovered = null
    sigma.refresh()
  }

  sigma.on('enterNode', onEnter)
  sigma.on('leaveNode', onLeave)

  return () => {
    sigma.off('enterNode', onEnter)
    sigma.off('leaveNode', onLeave)
    sigma.setSetting('nodeReducer', null)
    sigma.setSetting('edgeReducer', null)
  }
}

/** 点选节点或边，把选中项交给外部（详情面板用）。点空白处清除选择。 */
export function clickSelectionBehavior(
  sigma: Sigma,
  onSelect: (selection: { kind: 'node' | 'edge'; id: string } | null) => void,
): Dispose {
  const onClickNode = ({ node }: { node: string }) => onSelect({ kind: 'node', id: node })
  const onClickEdge = ({ edge }: { edge: string }) => onSelect({ kind: 'edge', id: edge })
  const onClickStage = () => onSelect(null)

  sigma.on('clickNode', onClickNode)
  sigma.on('clickEdge', onClickEdge)
  sigma.on('clickStage', onClickStage)

  return () => {
    sigma.off('clickNode', onClickNode)
    sigma.off('clickEdge', onClickEdge)
    sigma.off('clickStage', onClickStage)
  }
}

/**
 * 容器尺寸变化时重新适配画布。
 *
 * sigma 不会自己感知容器变化——密度切换、窗口缩放、侧边栏折叠都会让画布
 * 和容器错位，表现为图被裁掉一半或者留一大片空白。
 */
export function fitViewBehavior(sigma: Sigma, container: HTMLElement): Dispose {
  const refresh = () => {
    sigma.resize()
    sigma.getCamera().animatedReset({ duration: 0 })
  }
  const observer = new ResizeObserver(refresh)
  observer.observe(container)
  return () => observer.disconnect()
}
