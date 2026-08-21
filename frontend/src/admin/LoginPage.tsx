import { useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function LoginPage() {
  const { sessionToken, login } = useAdminAuth()
  const [adminToken, setAdminToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loggingIn, setLoggingIn] = useState(false)

  useEffect(() => {
    document.title = '管理后台登录 · 客服问答 Demo'
  }, [])

  if (sessionToken) {
    return <Navigate to="/admin" replace />
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setLoggingIn(true)
    try {
      await login(adminToken)
    } catch {
      setError('管理员 token 不正确')
    } finally {
      setLoggingIn(false)
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-paper px-4">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 border border-subtle bg-card p-6 shadow-soft"
      >
        <h1 className="text-xl font-bold text-ink">管理后台登录</h1>
        <label htmlFor="admin-token" className="text-sm font-bold text-ink">
          管理员 token
        </label>
        <input
          id="admin-token"
          type="password"
          autoComplete="current-password"
          value={adminToken}
          onChange={(event) => setAdminToken(event.target.value)}
          placeholder="管理员 token"
          disabled={loggingIn}
          className={`border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:shadow-soft focus:outline-none disabled:opacity-50 ${focusRing}`}
        />
        {error && (
          <p
            role="alert"
            className="border border-status-error bg-paper px-3 py-2 text-sm text-ink shadow-soft-sm"
          >
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={loggingIn || !adminToken}
          className={`min-h-[44px] cursor-pointer border border-subtle bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-soft transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
        >
          {loggingIn ? '登录中…' : '登录'}
        </button>
      </form>
    </div>
  )
}
