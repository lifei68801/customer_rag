import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useToast } from './ToastContext'
import { setCurrentTenantId, useAdminAuth } from './useAdminAuth'

/**
 * whoami 说当前租户是 null 时的兜底。admin 的 tenant_id 恒为 null，它的
 * 当前租户要显式切过一次才有值；在切成之前页面仍然要有一个租户去取数。
 * 真实库里未必存在叫 demo 的租户——useTenants 拉到租户列表后会把不在列表
 * 里的当前租户纠正掉。
 */
const FALLBACK_TENANT_ID = 'demo'

interface TenantContextValue {
  tenantId: string
  setTenantId: (next: string) => void
}

const TenantContext = createContext<TenantContextValue | null>(null)

/**
 * 当前操作租户的共享状态。
 *
 * 状态本身存在会话里（服务端），这里只是把它发给整棵子树。必须是 Context
 * 而不是普通 hook：租户下拉框（AccountMenu，渲染在 AdminLayout 的侧边栏里）
 * 和读取租户的页面（DocumentsPage / GraphReviewsPage，渲染在 <Outlet /> 里）
 * 是两棵不同的子树。
 *
 * 不再存 sessionStorage：sessionStorage 按标签页隔离，而会话 Cookie 是整个
 * 浏览器共享的——同一个人开两个标签页会看到两个不同的"当前租户"，而服务端
 * 只认一个。
 */
export function TenantProvider({ children }: { children: ReactNode }) {
  const { role, currentTenantId } = useAdminAuth()
  const showToast = useToast()
  const tenantId = currentTenantId ?? FALLBACK_TENANT_ID

  const value = useMemo<TenantContextValue>(
    () => ({
      tenantId,
      setTenantId: (next: string) => {
        // member 的租户是登录时绑定的，切换这个能力对它不存在——不是把
        // 按钮藏起来，是这个函数什么也不做。藏起来的按钮还能被别的代码
        // 路径调用到。真正的门在后端：member 请求别的租户会拿到 403。
        if (role !== 'admin') return
        void (async () => {
          try {
            const response = await adminFetch('/api/admin/auth/session/tenant', '', {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ tenant_id: next }),
            })
            if (!response.ok) {
              const body = await response.json().catch(() => ({}))
              showToast(extractErrorDetail(body, '切换租户失败'))
              return
            }
            // 先请求、成功了才更新本地状态。反过来的话请求失败时界面显示
            // 的租户和服务端生效的那个就对不上了，而后续每一次读写都按
            // 服务端那个走。
            setCurrentTenantId(next)
          } catch {
            // 请求压根没发出去（断网），或者 adminFetch 在 401 时抛了。切换是
            // 从菜单里发起的，菜单点完就关了，错误没有"原地"可停——不给反馈
            // 的话用户只会看到租户名没变，猜不到发生了什么。
            showToast('切换租户失败')
          }
        })()
      },
    }),
    [tenantId, role, showToast],
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
