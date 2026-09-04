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
    } catch {
      // 不比后端更具体：后端刻意不区分"用户不存在/密码错/账号禁用"，
      // 前端编一个更细的说法等于把那份克制作废。
      setError('用户名或密码不正确')
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
