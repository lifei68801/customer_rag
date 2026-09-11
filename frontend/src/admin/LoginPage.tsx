import { useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function LoginPage() {
  const { status, login } = useAdminAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)

  useEffect(() => {
    document.title = '管理后台登录 · 客服问答 Demo'
  }, [])

  // 会话状态未知时先不画：把登录表单闪给一个其实还登录着的人，他会以为
  // 自己被登出了。
  if (status === 'loading') {
    return null
  }
  if (status === 'authenticated') {
    return <Navigate to="/admin" replace />
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setLoggingIn(true)
    try {
      await login(username, password)
    } catch (err) {
      // 原样转达 useAdminAuth.login 给的消息，**不在这里另编一句**。
      //
      // 这里以前硬编码 '用户名或密码不正确'，把所有失败都说成凭据错误：
      // 后端没起来说成密码错（用户反复试密码，而真正的问题是服务没跑）、
      // 被限流锁 15 分钟也说成密码错（用户继续试，每次都在把锁定时间续上）。
      // 2026-09-11 真实踩到过前一种。
      //
      // "不比后端更具体"这条克制仍然成立，但它只适用于 401，现在由
      // useAdminAuth.login 在那一处兑现——同一句话散落两处的结果就是修了
      // 一处、另一处照旧覆盖掉。
      setError(err instanceof Error ? err.message : '登录失败，请稍后重试')
    } finally {
      setLoggingIn(false)
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-paper px-4">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-panel border border-subtle bg-card p-6"
      >
        <h1 className="font-mono text-xl font-semibold text-ink">管理后台登录</h1>
        {/* autoComplete 这两个值让浏览器和密码管理器认得出这是一对登录
            字段——写错的话每次登录都得手打。 */}
        <label htmlFor="admin-username" className="text-sm font-bold text-ink">
          用户名
        </label>
        <input
          id="admin-username"
          type="text"
          autoComplete="username"
          autoFocus
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          disabled={loggingIn}
          className={`rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
        />
        <label htmlFor="admin-password" className="text-sm font-bold text-ink">
          密码
        </label>
        <input
          id="admin-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={loggingIn}
          className={`rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
        />
        {error && (
          <p
            role="alert"
            className="rounded-card border border-status-error bg-paper px-3 py-2 text-sm text-ink"
          >
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={loggingIn || !username || !password}
          className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-5 py-2.5 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {loggingIn ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  )
}
