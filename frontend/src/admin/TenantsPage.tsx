import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Building2, Plus } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useConfirm } from './ConfirmContext'
import { useToast } from './ToastContext'

interface Tenant {
  tenant_id: string
  name: string
  status: 'active' | 'disabled'
}

const card = 'rounded-card border border-subtle bg-card p-4'
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

/**
 * 租户管理。只有 admin 能用。
 *
 * 它存在的直接理由：启动时会自动停用测试残留租户（见 app/auth/bootstrap.py），
 * 而此前没有任何界面能把它们启用回来——用户只能去调接口。
 *
 * 这一页是唯一会传 include_disabled=true 的地方。账号菜单里的切换下拉框
 * 用默认值（只列启用中的）：列出停用的租户会让人切过去之后发现读得到、
 * 写全是 404，那是最难查的一类状态。
 */
export function TenantsPage() {
  const { sessionToken, role } = useAdminAuth()
  const { tenantId: currentTenantId } = useAdminTenant()
  const confirm = useConfirm()
  const showToast = useToast()
  const [tenants, setTenants] = useState<Tenant[]>([])
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newId, setNewId] = useState('')
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState(false)

  const isAdmin = role === 'admin'

  const refresh = useCallback(async () => {
    if (!sessionToken || !isAdmin) return
    try {
      // 这一页必须看得到停用的租户——看不到就没法启用它们。
      const response = await adminFetch('/api/admin/tenants?include_disabled=true', sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载租户列表失败'))
      }
      const data = (await response.json()) as { tenants: Tenant[] }
      setTenants(data.tenants)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载租户列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, isAdmin])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    document.title = `${PAGE_TITLES.tenants} · 管理后台`
  }, [])

  if (!isAdmin) {
    return (
      <div data-testid="no-permission" className="flex flex-col gap-2">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.tenants}</h1>
        {/* 不用 404：404 会让人以为链接坏了而反复重试。说清是权限问题，
            人才知道该去找谁。 */}
        <p className="text-sm text-ink-soft">
          这个页面只有管理员能用。需要新建或停用租户，请联系管理员。
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
      const response = await adminFetch('/api/admin/tenants', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: newId.trim(), name: newName.trim() }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新建租户失败'))
      }
      setNewId('')
      setNewName('')
      setCreating(false)
      showToast('租户已创建')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '新建租户失败')
    } finally {
      setBusy(false)
    }
  }

  const handleToggleStatus = async (tenant: Tenant) => {
    if (!sessionToken) return
    const disabling = tenant.status === 'active'
    if (
      disabling &&
      !(await confirm({
        // 停用租户和停用账号的后果完全不同，必须说清楚：这里不是封停。
        // 现有策略是「读放行、写不放行」（见 app/api/tenant_guard.py），
        // 不说的话管理员会以为停用等于把人挡在门外。
        message:
          `停用「${tenant.name}」之后，属于它的成员**仍能登录、仍能读数据**，` +
          '但所有写操作（上传文档、批准关系、导入表格）都会失败。' +
          '这个租户也会从切换列表里消失。',
        confirmLabel: '停用',
      }))
    ) {
      return
    }
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/tenants/${encodeURIComponent(tenant.tenant_id)}/${
          disabling ? 'disable' : 'enable'
        }`,
        sessionToken,
        { method: 'POST' },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '操作失败'))
      }
      showToast(disabling ? '租户已停用' : '租户已启用')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    }
  }

  return (
    <div data-testid="tenants" className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.tenants}</h1>
        <p className="text-sm text-ink-soft">
          租户是数据作用域——每条实体、每篇文档、每个账号都属于某一个租户。
          租户只停用不删除：它的数据散在向量库、图谱和几个 SQLite 库里，删除是另一件事。
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
          <Plus aria-hidden="true" className="h-4 w-4" />
          新建租户
        </button>
      )}

      {creating && (
        <form onSubmit={handleCreate} className={`${card} flex max-w-md flex-col gap-3`}>
          <label htmlFor="new-tenant-id" className="text-sm font-bold text-ink">
            租户 ID
          </label>
          <input
            id="new-tenant-id"
            value={newId}
            onChange={(event) => setNewId(event.target.value)}
            autoFocus
            className={inputClass}
          />
          {/* ID 建完不能改：它是数据作用域的身份，散落在向量库、图谱和几个
              SQLite 库里，改一处等于把数据割裂开。 */}
          <p className="text-xs text-ink-soft">
            只能用字母、数字、下划线和连字符。建好之后**不能修改**——它是数据的归属标记。
          </p>
          <label htmlFor="new-tenant-name" className="text-sm font-bold text-ink">
            显示名
          </label>
          <input
            id="new-tenant-name"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
            className={inputClass}
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={busy || !newId.trim() || !newName.trim()}
              className={`${buttonClass} bg-accent-primary text-on-accent`}
            >
              {busy ? '创建中…' : '创建'}
            </button>
            <button type="button" onClick={() => setCreating(false)} className={buttonClass}>
              取消创建
            </button>
          </div>
        </form>
      )}

      {!loaded && <Skeleton variant="card-list" count={3} />}

      {loaded && tenants.length === 0 && (
        <EmptyState
          icon={Building2}
          title="还没有任何租户"
          action={<span>用上面的「新建租户」建第一个。</span>}
        />
      )}

      {loaded && tenants.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse text-sm">
            <thead>
              <tr className="border-b border-subtle text-left text-xs uppercase tracking-wide text-ink-soft">
                <th className="py-2 pr-4 font-bold">租户 ID</th>
                <th className="py-2 pr-4 font-bold">显示名</th>
                <th className="py-2 pr-4 font-bold">状态</th>
                <th className="py-2 font-bold">操作</th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((tenant) => (
                <tr key={tenant.tenant_id} className="border-b border-subtle">
                  <td className="py-2 pr-4">
                    <code className="font-mono text-ink">{tenant.tenant_id}</code>
                    {tenant.tenant_id === currentTenantId && (
                      <span className="ml-2 rounded-chip bg-accent-secondary px-2 py-0.5 text-xs font-bold text-on-accent">
                        当前
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-4 text-ink">{tenant.name}</td>
                  <td className="py-2 pr-4">
                    <span
                      className={`rounded-chip px-2 py-0.5 text-xs font-bold ${
                        tenant.status === 'active'
                          ? 'bg-status-success text-on-accent'
                          : 'bg-status-error text-on-accent'
                      }`}
                    >
                      {tenant.status === 'active' ? '启用' : '停用'}
                    </span>
                  </td>
                  <td className="py-2">
                    <button
                      type="button"
                      // 可访问名带上租户名：一列全是「停用」的按钮，屏幕
                      // 阅读器用户听不出点的是哪一行。
                      aria-label={`${tenant.status === 'active' ? '停用' : '启用'} ${tenant.tenant_id}`}
                      onClick={() => handleToggleStatus(tenant)}
                      className={buttonClass}
                    >
                      {tenant.status === 'active' ? '停用' : '启用'}
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
