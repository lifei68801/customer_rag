import { useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useToast } from './ToastContext'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink ${focusRing}`

/**
 * 修改自己的密码。
 *
 * 必须验旧密码——不验的话，任何拿到 session 的人（比如一台没锁屏的电脑）
 * 都能把这个账号锁给自己。
 */
export function ChangePassword() {
  const { sessionToken } = useAdminAuth()
  const showToast = useToast()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    // 两次不一致时不发请求：这个错误后端无从判断（它只收到一个新密码），
    // 发过去只会成功改成打错的那个，然后你就登不进来了。
    if (newPassword !== confirmPassword) {
      setError('两次输入的新密码不一致')
      return
    }
    if (!sessionToken) return
    setBusy(true)
    try {
      const response = await adminFetch('/api/admin/auth/password', sessionToken, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '修改密码失败'))
      }
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
      showToast('密码已修改')
    } catch (err) {
      setError(err instanceof Error ? err.message : '修改密码失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex max-w-md flex-col gap-3 rounded-card border border-subtle bg-card p-4"
    >
      <h2 className="font-mono text-sm font-bold uppercase tracking-wide text-ink-soft">
        修改密码
      </h2>
      <label htmlFor="old-password" className="text-sm font-bold text-ink">
        原密码
      </label>
      <input
        id="old-password"
        type="password"
        autoComplete="current-password"
        value={oldPassword}
        onChange={(event) => setOldPassword(event.target.value)}
        className={inputClass}
      />
      <label htmlFor="new-password" className="text-sm font-bold text-ink">
        新密码
      </label>
      <input
        id="new-password"
        type="password"
        autoComplete="new-password"
        value={newPassword}
        onChange={(event) => setNewPassword(event.target.value)}
        className={inputClass}
      />
      <label htmlFor="confirm-password" className="text-sm font-bold text-ink">
        确认新密码
      </label>
      <input
        id="confirm-password"
        type="password"
        autoComplete="new-password"
        value={confirmPassword}
        onChange={(event) => setConfirmPassword(event.target.value)}
        className={inputClass}
      />
      {error && (
        <p role="alert" className="text-sm text-status-error">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={busy || !oldPassword || !newPassword || !confirmPassword}
        className={`min-h-[36px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-3 text-sm font-bold text-on-accent transition disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
      >
        {busy ? '修改中…' : '修改密码'}
      </button>
    </form>
  )
}
