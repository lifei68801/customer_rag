/**
 * 导航上的待办数。
 *
 * 0 不渲染——每个链接后面挂一个 0 只是噪音，而"这里没事"本来就是默认
 * 预期，不需要专门说一遍。
 */
export function NavBadge({ label, count }: { label: string; count: number | undefined }) {
  if (!count) return null
  return (
    <span
      // 屏幕阅读器听到的是"待审关系：7 项待处理"，不是一个孤零零的"7"。
      aria-label={`${label}：${count} 项待处理`}
      className="ml-auto min-w-[1.25rem] rounded-chip bg-accent-secondary px-1.5 py-0.5 text-center text-xs font-bold text-on-accent"
    >
      {count > 99 ? '99+' : count}
    </span>
  )
}
