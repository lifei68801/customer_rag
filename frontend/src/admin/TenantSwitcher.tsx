import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

interface TenantOption {
  tenant_id: string
  name: string
  status: string
}

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

export function TenantSwitcher() {
  const { sessionToken } = useAdminAuth()
  const { tenantId, setTenantId } = useAdminTenant()
  const [tenants, setTenants] = useState<TenantOption[]>([])
  const [loaded, setLoaded] = useState(false)
  const [creating, setCreating] = useState(false)
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [newTenantId, setNewTenantId] = useState('')
  const [newTenantName, setNewTenantName] = useState('')
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!sessionToken) return
    try {
      const response = await adminFetch('/api/admin/tenants', sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '加载租户列表失败'))
      }
      const data = (await response.json()) as { tenants: TenantOption[] }
      setTenants(data.tenants)
      // TenantContext 的默认值（sessionStorage 缺失时回退到 'demo'）在真实
      // 有历史数据的库上并不可靠——ensure_tenants_schema 只在完全没有历史
      // 租户时才会回填 'demo'，所以当前 tenantId 很可能压根不在这次拉到的
      // 列表里，<select value={tenantId}> 会渲染成没有任何选中项、tenantId
      // 却悄悄停在一个不存在的值上，后续所有管理端写操作都会 404。这里一旦
      // 发现当前 tenantId 不在真实列表中（且列表非空），自动纠正到列表里的
      // 第一个租户——同样覆盖"当前所在租户被禁用掉了"的情况。
      if (data.tenants.length > 0 && !data.tenants.some((t) => t.tenant_id === tenantId)) {
        setTenantId(data.tenants[0].tenant_id)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载租户列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, setTenantId])

  useEffect(() => {
    refresh().catch((err) => console.error('租户列表刷新失败', err))
  }, [refresh])

  const handleCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || creating || !newTenantId.trim() || !newTenantName.trim()) return
    setError(null)
    setCreating(true)
    try {
      const response = await adminFetch('/api/admin/tenants', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: newTenantId.trim(), name: newTenantName.trim() }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新建租户失败'))
      }
      const created = (await response.json()) as TenantOption
      setTenants((prev) => [...prev, created])
      setTenantId(created.tenant_id)
      setNewTenantId('')
      setNewTenantName('')
      setShowCreateForm(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : '新建租户失败')
    } finally {
      setCreating(false)
    }
  }

  // 列表接口挂了的兜底：至少保留一个当前 tenantId 的选项，不让下拉框
  // 整个空掉、彻底没法操作——tenantId 本身来自 TenantContext 的
  // sessionStorage 缓存，即使租户列表拉取失败也还在。
  const options = loaded && tenants.length > 0 ? tenants : [{ tenant_id: tenantId, name: tenantId, status: 'active' }]

  return (
    <div className="flex flex-col gap-2">
      <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
        切换租户
        <select
          value={tenantId}
          onChange={(event) => setTenantId(event.target.value)}
          aria-label="切换租户"
          className="min-h-[44px] w-full rounded-control border border-subtle bg-paper px-2 text-sm font-bold text-ink"
        >
          {options.map((tenant) => (
            <option key={tenant.tenant_id} value={tenant.tenant_id}>
              {tenant.name}
            </option>
          ))}
        </select>
      </label>
      {error && <p role="alert" className="text-xs text-status-error">{error}</p>}
      {!showCreateForm && (
        <button
          type="button"
          onClick={() => setShowCreateForm(true)}
          className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-2 text-xs font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
        >
          + 新建租户
        </button>
      )}
      {showCreateForm && (
        <form onSubmit={handleCreate} className="flex flex-col gap-2 rounded-panel border border-subtle bg-paper p-2">
          <input
            value={newTenantId}
            onChange={(event) => setNewTenantId(event.target.value)}
            placeholder="tenant_id"
            aria-label="新租户 ID"
            className="rounded-control border border-subtle bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft focus:outline-none"
          />
          <input
            value={newTenantName}
            onChange={(event) => setNewTenantName(event.target.value)}
            placeholder="显示名"
            aria-label="新租户显示名"
            className="rounded-control border border-subtle bg-card px-2 py-1.5 text-xs text-ink placeholder:text-ink-soft focus:outline-none"
          />
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={creating || !newTenantId.trim() || !newTenantName.trim()}
              className={`min-h-[32px] flex-1 cursor-pointer rounded-control border border-subtle bg-accent-pink px-2 text-xs font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
            >
              {creating ? '创建中…' : '创建'}
            </button>
            <button
              type="button"
              onClick={() => {
                setShowCreateForm(false)
                setNewTenantId('')
                setNewTenantName('')
                setError(null)
              }}
              disabled={creating}
              className={`min-h-[32px] cursor-pointer rounded-control border border-subtle bg-card px-2 text-xs font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
            >
              取消
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
