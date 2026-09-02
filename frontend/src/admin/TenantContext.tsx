import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { useAdminAuth } from './useAdminAuth'

const TENANT_STORAGE_KEY = 'admin_current_tenant'

interface TenantContextValue {
  tenantId: string
  setTenantId: (next: string) => void
}

const TenantContext = createContext<TenantContextValue | null>(null)

/**
 * 当前操作租户的共享状态。
 *
 * 必须是 Context 而不是普通 hook：租户下拉框（TenantSwitcher，渲染在
 * AdminLayout 的侧边栏里）和读取租户的页面（DocumentsPage /
 * GraphReviewsPage，渲染在 <Outlet /> 里）是两棵不同的子树，各自 useState
 * 会拿到互不相干的状态格——切下拉框只会更新 sessionStorage 和它自己，
 * 页面既不会重渲染也不会重新拉数据。
 */
export function TenantProvider({ children }: { children: ReactNode }) {
  const { role } = useAdminAuth()
  const [tenantId, setTenantIdState] = useState(
    () => sessionStorage.getItem(TENANT_STORAGE_KEY) ?? 'demo',
  )

  const value = useMemo<TenantContextValue>(
    () => ({
      tenantId,
      setTenantId: (next: string) => {
        // member 的租户是登录时绑定的，切换这个能力对它不存在——不是把
        // 按钮藏起来，是这个函数什么也不做。藏起来的按钮还能被别的代码
        // 路径调用到。真正的门在后端：member 请求别的租户会拿到 403。
        if (role !== 'admin') return
        sessionStorage.setItem(TENANT_STORAGE_KEY, next)
        setTenantIdState(next)
      },
    }),
    [tenantId, role],
  )

  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
}

export function useAdminTenant(): TenantContextValue {
  const value = useContext(TenantContext)
  if (value === null) {
    throw new Error('useAdminTenant() 必须在 <TenantProvider> 内部使用')
  }
  return value
}
