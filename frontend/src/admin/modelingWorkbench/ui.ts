// 跟 GuidedOntologyPage 用的是同一组类名——工作台替换的是那一页，样式不该
// 在替换过程中发生无关的变化。
export const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export const primaryButtonClass = `min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

export const secondaryButtonClass = `inline-flex min-h-[44px] cursor-pointer items-center rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition hover:bg-interactive-hover active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

export const panelClass = 'rounded-panel border border-subtle bg-paper p-4'

export const tagClass = 'rounded-control border border-subtle px-2 py-0.5 text-xs text-ink-soft'
