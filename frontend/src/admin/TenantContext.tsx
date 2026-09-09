import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
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
  /**
   * 切当前租户，并告诉调用方切成功了没有。
   *
   * `setTenantId` 是发后不管的，调用方拿不到结果。看板需要这个：它是
   * **跨领域**的，用户当前挂在 A、点的是 B 那张卡上的待办，必须先把当前
   * 租户切成 B 再跳，否则他落在审核页上看到的是 A 的队列——数字是 B 的、
   * 内容是 A 的，而界面全程不说话。
   */
  switchTenant: (next: string) => Promise<boolean>
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
  const { currentTenantId } = useAdminAuth()
  const showToast = useToast()
  const tenantId = currentTenantId ?? FALLBACK_TENANT_ID

  // 两个入口共用一份实现：写成两份的话，它们会在"切了但没生效"这个 bug
  // 上分叉。
  const switchTenant = useCallback(
    async (next: string): Promise<boolean> => {
      try {
        const response = await adminFetch('/api/admin/auth/session/tenant', '', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ tenant_id: next }),
        })
        if (!response.ok) {
          const body = await response.json().catch(() => ({}))
          showToast(extractErrorDetail(body, '切换租户失败'))
          return false
        }
        // 先请求、成功了才更新本地状态。反过来的话请求失败时界面显示的
        // 租户和服务端生效的那个就对不上了，而后续每一次读写都按服务端
        // 那个走。
        setCurrentTenantId(next)
        return true
      } catch {
        // 请求压根没发出去（断网），或者 adminFetch 在 401 时抛了。从菜单
        // 发起的切换点完菜单就关了，错误没有"原地"可停——不给反馈的话
        // 用户只会看到租户名没变，猜不到发生了什么。
        showToast('切换租户失败')
        return false
      }
    },
    [showToast],
  )

  const value = useMemo<TenantContextValue>(
    () => ({
      tenantId,
      switchTenant,
      // 此前这里有一道 `if (role !== 'admin') return`：member 的租户曾经是
      // 登录时绑定的单一一列，切换对它没有意义。user_tenants 表上线后
      // （app/auth/user_tenants_store.py），member 可以被显式授权访问多个
      // 租户——这道前端闸门就变成了"挡住一个后端已经允许的操作"。
      //
      // 去掉它不放宽任何边界：真正的门在后端，assert_tenant_accessible
      // （app/api/deps.py）按 user_tenants 里的显式授权判断，是这个 PUT
      // 路由和所有租户内路由共用的同一个函数。member 切到没被授权的租户
      // 会拿到 403，走上面的失败分支给出 toast。
      setTenantId: (next: string) => {
        void switchTenant(next)
      },
    }),
    [tenantId, switchTenant],
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
