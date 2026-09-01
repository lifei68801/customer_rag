import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'

export interface TenantOption {
  tenant_id: string
  name: string
  status: string
}

/**
 * 租户列表 + 新建。
 *
 * 从 TenantSwitcher 抽出来的：租户切换搬进账号菜单之后，取数逻辑和渲染
 * 得分开，否则菜单要连着那个下拉框的样式一起继承。
 */
export function useTenants() {
  const { sessionToken } = useAdminAuth()
  const { tenantId, setTenantId } = useAdminTenant()
  const [tenants, setTenants] = useState<TenantOption[]>([])
  const [loaded, setLoaded] = useState(false)
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
      // 列表里，界面会显示一个不存在的租户名、而后续所有写操作都会 404。
      // 一旦发现当前 tenantId 不在真实列表中（且列表非空），自动纠正到列表
      // 里的第一个——同样覆盖"当前所在租户被禁用掉了"的情况。
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

  const create = useCallback(
    async (id: string, name: string) => {
      if (!sessionToken) return
      setError(null)
      const response = await adminFetch('/api/admin/tenants', sessionToken, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: id, name }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '新建租户失败'))
      }
      const created = (await response.json()) as TenantOption
      setTenants((prev) => [...prev, created])
      // 建完就切过去：新建一个租户的意图就是要用它。
      setTenantId(created.tenant_id)
    },
    [sessionToken, setTenantId],
  )

  // 列表接口挂了的兜底：至少保留当前 tenantId 这一项，不让菜单整个空掉、
  // 彻底没法操作——tenantId 来自 TenantContext 的 sessionStorage 缓存，
  // 即使列表拉取失败也还在。
  const options =
    loaded && tenants.length > 0 ? tenants : [{ tenant_id: tenantId, name: tenantId, status: 'active' }]

  const current = options.find((t) => t.tenant_id === tenantId)

  return { options, current, tenantId, setTenantId, create, error, setError }
}
