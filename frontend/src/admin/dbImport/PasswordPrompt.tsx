import { useEffect, useRef, useState, type FormEvent } from 'react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

/**
 * 重新同步时现填密码。
 *
 * 密码不在库里（spec D3），所以每次同步都要问一次。**输完立刻从 state 里
 * 清掉**，也绝不写进 localStorage / sessionStorage——记住它等于把「库里零
 * 密码」这个承诺在浏览器一侧作废了一半：那两个存储在同源下是任何脚本都读
 * 得到的明文。
 *
 * 值只通过请求体传给后端，绝不进 URL：URL 会进浏览器历史、进服务端访问
 * 日志、进 Referer 头。
 */
export function PasswordPrompt({
  sourceName,
  onSubmit,
  onCancel,
}: {
  sourceName: string
  onSubmit: (password: string) => void
  onCancel: () => void
}) {
  const [password, setPassword] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    onSubmit(password)
    // 交出去就清掉。留在 state 里的话，这一页只要还开着，密码就一直在
    // 内存里躺着，任何一次组件树的 dump（错误上报、调试插件）都会带上它。
    setPassword('')
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`同步「${sourceName}」需要密码`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
    >
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-3 rounded-card border border-subtle bg-paper p-4"
      >
        <p className="text-sm text-ink">
          同步「{sourceName}」需要数据库密码。
          <span className="text-ink-soft">密码不会被保存，每次同步都要重新输。</span>
        </p>
        <label className="flex flex-col gap-1 text-sm text-ink">
          密码
          <input
            ref={inputRef}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className={`rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink ${focusRing}`}
          />
        </label>
        <div className="flex justify-end gap-2">
          <button type="button" className={buttonClass} onClick={onCancel}>
            取消
          </button>
          <button type="submit" className={buttonClass}>
            开始同步
          </button>
        </div>
      </form>
    </div>
  )
}
