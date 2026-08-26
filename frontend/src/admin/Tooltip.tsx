import { useEffect, useRef, useState, type ReactNode } from 'react'

interface TooltipProps {
  label: string
  children: ReactNode
}

const SHOW_DELAY_MS = 150

/**
 * 固定在子元素正上方展开的提示——只用在全项目仅有的 2 处纯图标控件
 * （没有可见文字，只能靠这个补充视觉提示）：ChatSidebar 的删除会话
 * 图标、Pager 的上一页/下一页箭头。不做防溢出智能定位：这 2 处控件
 * 位置固定，不存在被视口边缘遮挡的情况。子元素自身的 aria-label 不受
 * 影响，两者并存：aria-label 给屏幕阅读器，这个提示给视觉用户。
 *
 * mounted 控制"延迟 150ms 后要不要渲染这个提示"，visible 控制"渲染出来
 * 之后的下一帧再把透明度拉到 100%"——拆成两个状态是为了让 CSS
 * transition 真正播放：如果一次性用 opacity-100 挂载，浏览器不会补一帧
 * opacity-0 的初始状态，transition 就没有起点可过渡，直接瞬间出现。
 */
export function Tooltip({ label, children }: TooltipProps) {
  const [mounted, setMounted] = useState(false)
  const [visible, setVisible] = useState(false)
  const showTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!mounted) return
    const frame = requestAnimationFrame(() => setVisible(true))
    return () => cancelAnimationFrame(frame)
  }, [mounted])

  const show = () => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current)
    showTimerRef.current = setTimeout(() => setMounted(true), SHOW_DELAY_MS)
  }

  const hide = () => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current)
    setMounted(false)
    setVisible(false)
  }

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {mounted && (
        <span
          role="tooltip"
          className={`pointer-events-none absolute bottom-full left-1/2 z-40 mb-1.5 -translate-x-1/2 whitespace-nowrap rounded-modal border border-subtle bg-ink px-2 py-1 text-xs font-bold text-paper transition-opacity duration-150 motion-reduce:transition-none ${
            visible ? 'opacity-100' : 'opacity-0'
          }`}
        >
          {label}
        </span>
      )}
    </span>
  )
}
