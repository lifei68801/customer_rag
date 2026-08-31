/**
 * 本体图的取色与尺寸。
 *
 * 全部从页面已有的 CSS 变量读取——图不能自带一套配色，否则换肤
 * （SkinContext）时图会跟页面其余部分脱节。sigma 需要具体的颜色字符串、
 * 不认 CSS 变量，所以这里在渲染前把变量解析成实际值。
 */

/** 读一个 `--color-*` 变量并转成 sigma 能用的 rgb() 字符串。 */
function readColor(name: string, fallback: string): string {
  if (typeof window === 'undefined') return fallback
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  if (!raw) return fallback
  // 令牌存的是 "31 41 55" 这样的裸通道值（配合 Tailwind 的 <alpha-value>），
  // 也可能是已经完整的颜色字符串，两种都要认。
  return /^[\d\s.]+$/.test(raw) ? `rgb(${raw.split(/\s+/).join(', ')})` : raw
}

export interface GraphTheme {
  node: string
  nodeLabel: string
  edge: string
  /** 扇形陷阱风险边（一对多）的颜色，用语义 error 令牌。 */
  edgeRisky: string
  highlight: string
  dimmed: string
}

export function readGraphTheme(): GraphTheme {
  return {
    node: readColor('--color-accent-primary', 'rgb(37, 99, 235)'),
    nodeLabel: readColor('--color-ink', 'rgb(17, 24, 39)'),
    edge: readColor('--color-ink-soft', 'rgb(107, 114, 128)'),
    edgeRisky: readColor('--color-status-error-strong', 'rgb(185, 28, 28)'),
    highlight: readColor('--color-accent-secondary', 'rgb(217, 119, 6)'),
    dimmed: 'rgba(148, 163, 184, 0.25)',
  }
}

/** 节点半径按它的连接数缩放：度数高的类型在图上更显眼。 */
export function nodeSize(degree: number): number {
  return 8 + Math.min(degree, 8) * 1.5
}

export const EDGE_SIZE = 2
export const EDGE_SIZE_RISKY = 3.5
