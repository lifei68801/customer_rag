interface SkeletonProps {
  variant: 'table-rows' | 'card-list'
  count?: number
}

const ROW_WIDTHS = ['60%', '35%', '45%']
const CARD_WIDTHS = ['55%', '30%']

/**
 * 加载态占位——用方块骨架而不是纯文字"加载中…"，让加载前后的高度基本
 * 一致，避免内容到达时整块跳动（CLS）。不用居中的圆形旋转 spinner：
 * spinner 只是一个跟内容无关的符号，撑不出真实布局，仍然会在数据到达时
 * 跳动；方块骨架直接复刻表格行/卡片的形状和尺寸。动效用 Tailwind 内置
 * animate-pulse（透明度 1↔0.5 循环）。
 */
export function Skeleton({ variant, count = 3 }: SkeletonProps) {
  if (variant === 'table-rows') {
    return (
      <div className="overflow-x-auto rounded-card border border-subtle bg-card shadow-soft-sm" aria-hidden="true">
        {Array.from({ length: count }, (_, row) => (
          <div key={row} className="flex items-center gap-4 border-b border-ink/20 px-3 py-2 last:border-b-0">
            {ROW_WIDTHS.map((width, col) => (
              <div key={col} className="h-4 animate-pulse motion-reduce:animate-none bg-ink-soft/40" style={{ width }} />
            ))}
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2" aria-hidden="true">
      {Array.from({ length: count }, (_, card) => (
        <div key={card} className="flex flex-col gap-2 rounded-card border border-subtle bg-card p-4 shadow-soft-sm">
          {CARD_WIDTHS.map((width, line) => (
            <div key={line} className="h-4 animate-pulse motion-reduce:animate-none bg-ink-soft/40" style={{ width }} />
          ))}
        </div>
      ))}
    </div>
  )
}
