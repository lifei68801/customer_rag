type BadgeTone = 'neutral' | 'active' | 'success' | 'error' | 'warning'

interface TaskStatusBadgeProps {
  tone: BadgeTone
  label: string
}

// 调用方负责把自己领域里的原始状态字符串（'running'/'pending'/'approved' 等，
// 每个页面的取值集合都不一样）映射成这四种统一的语气 + 展示文案，这个组件
// 本身不认识任何具体业务状态值，只负责统一视觉呈现。
const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: 'border-subtle bg-paper text-ink',
  active: 'border-subtle bg-accent-cyan text-ink',
  success: 'border-status-success bg-paper text-status-success',
  error: 'border-status-error bg-paper text-status-error',
  warning: 'border-subtle bg-accent-yellow text-ink',
}

export function TaskStatusBadge({ tone, label }: TaskStatusBadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-chip border px-2 py-0.5 text-xs font-bold ${TONE_CLASS[tone]}`}
    >
      {tone === 'active' && (
        <span
          aria-hidden="true"
          className="h-2 w-2 flex-shrink-0 animate-pulse bg-ink motion-reduce:animate-none"
        />
      )}
      {label}
    </span>
  )
}
