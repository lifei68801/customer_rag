/**
 * 导航上的计数。
 *
 * 两种语义，两种长相：
 * - todo（默认）：有多少件事等着你。实心强调色，看见就该去处理。
 * - scale：这里有多大。灰色无底色，是个事实不是催促。
 *
 * 分开不是装饰。用同样的样式，「20017 条实体」会稀释「7 条待审关系」的
 * 意义——后者才是真的有事等着你。
 */
export function NavBadge({
  label,
  count,
  kind = 'todo',
}: {
  label: string
  count: number | undefined
  kind?: 'todo' | 'scale'
}) {
  // 0 不渲染：每个链接后面挂一个 0 只是噪音，而「这里没事」本来就是默认
  // 预期，不需要专门说一遍。空租户也不需要被提醒它是空的。
  if (!count) return null

  const scale = kind === 'scale'
  return (
    <span
      // 屏幕阅读器听到的是「待审关系：7 项待处理」或「实体列表：共 20,017 条」，
      // 不是一个孤零零的数字。
      aria-label={
        scale ? `${label}：共 ${count.toLocaleString()} 条` : `${label}：${count} 项待处理`
      }
      className={
        scale
          ? 'ml-auto text-xs font-mono tabular-nums text-ink-soft'
          : 'ml-auto min-w-[1.25rem] rounded-chip bg-accent-secondary px-1.5 py-0.5 text-center text-xs font-bold text-on-accent'
      }
    >
      {scale ? count.toLocaleString() : count > 99 ? '99+' : count}
    </span>
  )
}
