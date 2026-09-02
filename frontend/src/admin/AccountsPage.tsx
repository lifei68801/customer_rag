import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { UserPlus, Users } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useTenants } from './useTenants'
import { useConfirm } from './ConfirmContext'
import { useToast } from './ToastContext'

interface Account {
  username: string
  role: 'admin' | 'member'
  tenant_id: string | null
  status: 'active' | 'disabled'
  created_at: string
  last_login_at: string | null
}

const card = 'rounded-card border border-subtle bg-card p-4'
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

/**
 * 账号管理。只有 admin 能用。
 *
 * 账号只停用不删除：这个系统里的写操作（删文档、批准关系入 Neo4j）不可逆，
 * 账号删了之后"这批数据是谁批准的"就永远查不出来了。
 */
export function AccountsPage() {
  const { sessionToken, role, username: self } = useAdminAuth()
  const { options: tenants } = useTenants()
  const confirm = useConfirm()
  const showToast = useToast()
  const [accounts, setAccounts] = useState<Account[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newTenant, setNewTenant] = useState('')
  const [busy, setBusy] = useState(false)
  // 正在给谁重置密码。null = 没在重置。
  const [resetting, setResetting] = useState<string | null>(null)
  const [resetPassword, setResetPassword] = useState('')

  const isAdmin = role === 'admin'

  const refresh = useCallback(async () => {
    if (!sessionToken || !isAdmin) return
    try {
      const response = await adminFetch('/api/admin/accounts', sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载账号列表失败'))
      }
      const data = (await response.json()) as { accounts: Account[] }
      setAccounts(data.accounts)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载账号列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, isAdmin])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    document.title = `${PAGE_TITLES.accounts} · 管理后台`
  }, [])

  // 权限判断放在取数之后、渲染之前：member 连列表请求都不会发出去（refresh
  // 里就挡住了），那个请求只会拿回 403。
  if (!isAdmin) {
    return (
      <div data-testid="no-permission" className="flex flex-col gap-2">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.accounts}</h1>
        {/* 不用 404：404 会让人以为链接坏了而反复重试。说清是权限问题，
            人才知道该去找谁。 */}
        <p className="text-sm text-ink-soft">
          这个页面只有管理员能用。需要新建或停用账号，请联系管理员。
        </p>
      </div>
    )
  }

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || busy) return
    setBusy(true)
    setError(null)
    try {
      const response = await adminFetch('/api/admin/accounts', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: newUsername.trim(),
          password: newPassword,
          tenant_id: newTenant,
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新建账号失败'))
      }
      setNewUsername('')
      setNewPassword('')
      setCreating(false)
      showToast('账号已创建')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新建账号失败')
    } finally {
      setBusy(false)
    }
  }

  const handleToggleStatus = async (account: Account) => {
    if (!sessionToken) return
    const disabling = account.status === 'active'
    if (
      disabling &&
      !(await confirm({
        message:
          `停用「${account.username}」之后，这个账号立刻无法登录，` +
          '正在进行的操作会中断。',
        confirmLabel: '停用',
      }))
    ) {
      return
    }
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/accounts/${encodeURIComponent(account.username)}/${
          disabling ? 'disable' : 'enable'
        }`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '操作失败'))
      }
      showToast(disabling ? '账号已停用' : '账号已启用')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    }
  }

  const handleReset = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || !resetting || busy) return
    setBusy(true)
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/accounts/${encodeURIComponent(resetting)}/password`,
        sessionToken,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ new_password: resetPassword }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重置密码失败'))
      }
      setResetting(null)
      setResetPassword('')
      showToast('密码已重置')
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置密码失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-testid="accounts" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.accounts}</h1>
        <p className="text-sm text-ink-soft">
          每个账号绑定一个租户，登录后只能看到那个租户的数据。账号只停用不删除——
          删了之后「这批数据是谁批准的」就查不出来了。
        </p>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink"
        >
          {error}
        </p>
      )}

      {!creating && (
        <button
          type="button"
          onClick={() => setCreating(true)}
          className={`flex items-center gap-1.5 self-start ${buttonClass}`}
        >
          <UserPlus aria-hidden="true" className="h-4 w-4" />
          新建账号
        </button>
      )}

      {creating && (
        <form onSubmit={handleCreate} className={`${card} flex max-w-md flex-col gap-3`}>
          <label htmlFor="new-username" className="text-sm font-bold text-ink">
            用户名
          </label>
          <input
            id="new-username"
            value={newUsername}
            onChange={(event) => setNewUsername(event.target.value)}
            autoFocus
            className={inputClass}
          />
          <label htmlFor="new-password" className="text-sm font-bold text-ink">
            初始密码
          </label>
          <input
            id="new-password"
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            className={inputClass}
          />
          <label htmlFor="new-tenant" className="text-sm font-bold text-ink">
            所属租户
          </label>
          {/* 必须选租户：建给不存在的租户，那个账号登录后会看到一片空白，
              而且没人说得出为什么。 */}
          <select
            id="new-tenant"
            value={newTenant}
            onChange={(event) => setNewTenant(event.target.value)}
            className={inputClass}
          >
            <option value="">请选择</option>
            {tenants.map((tenant) => (
              <option key={tenant.tenant_id} value={tenant.tenant_id}>
                {tenant.name}
              </option>
            ))}
          </select>
          <p className="text-xs text-ink-soft">
            初始密码由你当面交给对方——系统不会替你发送。对方登录后可以在设置页自行修改。
          </p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || !newUsername.trim() || !newPassword || !newTenant}
              className={`${buttonClass} bg-accent-primary text-on-accent`}
            >
              {busy ? '创建中…' : '创建'}
            </button>
            <button type="button" onClick={() => setCreating(false)} className={buttonClass}>
              取消
            </button>
          </div>
        </form>
      )}

      {resetting && (
        <form onSubmit={handleReset} className={`${card} flex max-w-md flex-col gap-3`}>
          <h2 className="font-mono text-sm font-bold uppercase tracking-wide text-ink-soft">
            重置「{resetting}」的密码
          </h2>
          <label htmlFor="reset-password" className="text-sm font-bold text-ink">
            新密码
          </label>
          <input
            id="reset-password"
            type="password"
            autoComplete="new-password"
            value={resetPassword}
            onChange={(event) => setResetPassword(event.target.value)}
            autoFocus
            className={inputClass}
          />
          <p className="text-xs text-ink-soft">
            新密码由你当面交给对方——系统不会替你发送。对方登录后可以在设置页自行修改。
          </p>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || !resetPassword}
              className={`${buttonClass} bg-accent-primary text-on-accent`}
            >
              {busy ? '重置中…' : '确认重置'}
            </button>
            <button type="button" onClick={() => setResetting(null)} className={buttonClass}>
              取消重置
            </button>
          </div>
        </form>
      )}

      {!loaded && <Skeleton variant="card-list" count={3} />}

      {loaded && accounts.length === 0 && (
        <EmptyState
          icon={Users}
          title="还没有任何账号"
          action={<span>用上面的「新建账号」给某个租户建第一个账号。</span>}
        />
      )}

      {loaded && accounts.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-subtle text-left text-xs uppercase tracking-wide text-ink-soft">
                <th className="py-2 pr-4 font-bold">用户名</th>
                <th className="py-2 pr-4 font-bold">角色</th>
                <th className="py-2 pr-4 font-bold">所属租户</th>
                <th className="py-2 pr-4 font-bold">状态</th>
                <th className="py-2 pr-4 font-bold">最后登录</th>
                <th className="py-2 font-bold">操作</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((account) => (
                <tr key={account.username} className="border-b border-subtle">
                  <td className="py-2 pr-4 font-bold text-ink">{account.username}</td>
                  <td className="py-2 pr-4 text-ink-soft">
                    {account.role === 'admin' ? '管理员' : '成员'}
                  </td>
                  <td className="py-2 pr-4 text-ink-soft">{account.tenant_id ?? '全部'}</td>
                  <td className="py-2 pr-4">
                    <span
                      className={`rounded-chip px-2 py-0.5 text-xs font-bold ${
                        account.status === 'active'
                          ? 'bg-status-success text-on-accent'
                          : 'bg-status-error text-on-accent'
                      }`}
                    >
                      {account.status === 'active' ? '启用' : '停用'}
                    </span>
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs text-ink-faint">
                    {/* 从没登录过和"很久没登录"是两回事，分开说。 */}
                    {account.last_login_at ?? '从未登录'}
                  </td>
                  <td className="py-2">
                    <button
                      type="button"
                      // 可访问名带上用户名：一列全是「停用」的按钮，屏幕
                      // 阅读器用户听不出点的是哪一行。
                      aria-label={`${account.status === 'active' ? '停用' : '启用'} ${account.username}`}
                      onClick={() => handleToggleStatus(account)}
                      disabled={account.username === self}
                      // 后端也会拒（返回 400），前端禁用只是不让人白点一次。
                      title={account.username === self ? '不能停用自己' : undefined}
                      className={buttonClass}
                    >
                      {account.status === 'active' ? '停用' : '启用'}
                    </button>
                    {/* 重置不需要旧密码——这个按钮就是给"忘了密码"用的。
                        没有它，admin 在界面上帮不了忘记密码的人，只能手改
                        数据库。 */}
                    <button
                      type="button"
                      aria-label={`重置 ${account.username} 的密码`}
                      onClick={() => {
                        setResetting(account.username)
                        setResetPassword('')
                      }}
                      className={`ml-2 ${buttonClass}`}
                    >
                      重置密码
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
