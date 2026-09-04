import { useCallback, useEffect, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'

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
  const showToast = useToast()
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
      // 会话里确实有一个当前租户，但它不在这次拉到的列表里——要么这个
      // 租户压根不存在（TenantContext 的兜底值 'demo' 在有历史数据的库上
      // 并不可靠：ensure_tenants_schema 只在完全没有历史租户时才回填它），
      // 要么它已经被停用（这个接口只列启用中的，两种情况在这里是同一个
      // 形状）。不纠正的话界面会显示一个不存在的租户名，而后续所有写操作
      // 都 404。
      //
      // 这一种可以自动纠正，因为系统**知道**该去哪：当前这个无效了，换个
      // 有效的。但换完必须说出来——用户没发起任何动作，作用域却变了，
      // 不说的话他只能从账号块按钮上的名字发现。
      //
      // currentTenantId === null 刻意不在这里：那时系统不知道用户想去哪，
      // 「列表里的第一个」跟「他想用哪个」毫无关系。替他选的后果是他以为
      // 自己在主数据租户里、实际在示例数据租户里，然后被一条从没见过的
      // 数据挡住。那一路交给 AdminLayout 的空态，让用户自己选。
      const missing = !data.tenants.some((t) => t.tenant_id === tenantId)
      if (currentTenantId !== null && data.tenants.length > 0 && missing) {
        const next = data.tenants[0]
        setTenantId(next.tenant_id)
        showToast(`租户 ${currentTenantId} 不可用（不存在或已停用），已切换到 ${next.name}`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载租户列表失败')
    } finally {
      setLoaded(true)
    }
  }, [sessionToken, tenantId, currentTenantId, setTenantId, showToast])

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
