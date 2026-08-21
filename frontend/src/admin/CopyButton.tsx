import { useState } from 'react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface CopyButtonProps {
  getText: () => string
  label?: string
}

export function CopyButton({ getText, label = '复制' }: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  const handleClick = async () => {
    try {
      await navigator.clipboard.writeText(getText())
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // 剪贴板权限被拒绝/浏览器不支持时静默失败——复制是锦上添花的辅助
      // 功能，用户本来就可以手动选中文本复制，不需要额外弹错误提示打断
      // 操作。
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      className={`self-start rounded-control border border-subtle bg-paper px-3 py-1.5 text-xs font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
    >
      {copied ? '已复制' : label}
    </button>
  )
}
