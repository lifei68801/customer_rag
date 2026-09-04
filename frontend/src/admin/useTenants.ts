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
 * 租户列表（只有启用中的）。
 *
 * 从 TenantSwitcher 抽出来的：租户切换搬进账号菜单之后，取数逻辑和渲染
 * 得分开，否则菜单要连着那个下拉框的样式一起继承。
 *
 * 只读。新建/停用租户在「租户管理」页，那一页直接调接口——它还要看到
 * 停用的租户（include_disabled=true），跟这里的用途正好相反：这里的列表
 * 是给"切到哪个租户去工作"用的，列出停用的会让人切过去之后发现写操作
 * 全是 404。
 */
export function useTenants() {
  const { sessionToken, currentTenantId } = useAdminAuth()
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
      // TenantContext 的兜底值（whoami 说当前租户是 null 时回退到 'demo'）
      // 在真实有历史数据的库上并不可靠——ensure_tenants_schema 只在完全没有
      // 历史租户时才会回填 'demo'，所以当前 tenantId 很可能压根不在这次拉到
      // 的列表里，界面会显示一个不存在的租户名、而后续所有写操作都会 404。
      // 一旦发现当前 tenantId 不在真实列表中（且列表非空），自动纠正到列表
      // 里的第一个——同样覆盖"当前所在租户被禁用掉了"的情况。
      //
      // current_tenant_id 是 null 时同样要纠正：那时界面上显示的是兜底值，
      // 服务端会话里却什么都没有，两边说的不是一回事。
      if (
        data.tenants.length > 0 &&
        (currentTenantId === null || !data.tenants.some((t) => t.tenant_id === tenantId))
      ) {
        setTenantId(data.tenants[0].tenant_id)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载租户列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, currentTenantId, setTenantId])

  useEffect(() => {
    refresh().catch((err) => console.error('租户列表刷新失败', err))
  }, [refresh])

  // 列表接口挂了的兜底：至少保留当前 tenantId 这一项，不让菜单整个空掉、
  // 彻底没法操作——tenantId 来自会话状态，即使列表拉取失败也还在。
  const options =
    loaded && tenants.length > 0 ? tenants : [{ tenant_id: tenantId, name: tenantId, status: 'active' }]

  const current = options.find((t) => t.tenant_id === tenantId)

  return { options, current, tenantId, setTenantId, error }
}
