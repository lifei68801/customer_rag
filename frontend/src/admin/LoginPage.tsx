import { useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'

export function LoginPage() {
  const { sessionToken, login } = useAdminAuth()
  const [adminToken, setAdminToken] = useState('')
  const [error, setError] = useState<string | null>(null)

  if (sessionToken) {
    return <Navigate to="/admin" replace />
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    try {
      await login(adminToken)
    } catch {
      setError('管理员 token 不正确')
    }
  }

  return (
    <div className="flex min-h-dvh flex-col items-center justify-center bg-paper px-4">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 border-2 border-ink bg-card p-6 shadow-brutal"
      >
        <h1 className="text-xl font-bold text-ink">管理后台登录</h1>
        <input
          type="password"
          value={adminToken}
          onChange={(event) => setAdminToken(event.target.value)}
          placeholder="管理员 token"
          className="border-2 border-ink bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:shadow-brutal focus:outline-none"
        />
        {error && <p className="text-sm text-ink">{error}</p>}
        <button
          type="submit"
          className="cursor-pointer border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none"
        >
          登录
        </button>
      </form>
    </div>
  )
}
