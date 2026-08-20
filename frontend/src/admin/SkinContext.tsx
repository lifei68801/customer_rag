import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

const SKIN_STORAGE_KEY = 'admin_skin'

export type SkinId = 'default' | 'dark' | 'business-blue'

const VALID_SKIN_IDS: readonly SkinId[] = ['default', 'dark', 'business-blue']

function isSkinId(value: string | null): value is SkinId {
  return value !== null && (VALID_SKIN_IDS as readonly string[]).includes(value)
}

interface SkinContextValue {
  skin: SkinId
  setSkin: (next: SkinId) => void
}

const SkinContext = createContext<SkinContextValue | null>(null)

/**
 * 当前选择的配色皮肤——站点级个人偏好（前台聊天页 + 后台管理共用同一份），
 * 存 localStorage（不是 TenantContext 用的 sessionStorage：皮肤偏好要跨
 * 浏览器会话保留，不像"当前操作哪个租户"那样是会话级状态）。用 Context 是
 * 为了跟 TenantContext 保持同一套架构模式。SkinProvider 挂载在 main.tsx
 * 的根节点，而不是只挂在 AdminLayout 下——否则前台路由永远拿不到这个
 * Provider，data-skin 属性也就永远不会被设置。
 */
export function SkinProvider({ children }: { children: ReactNode }) {
  const [skin, setSkinState] = useState<SkinId>(() => {
    const stored = localStorage.getItem(SKIN_STORAGE_KEY)
    return isSkinId(stored) ? stored : 'default'
  })

  // 把当前皮肤同步到 <html data-skin="..."> 上——index.css 里的
  // :root[data-skin="dark"] / :root[data-skin="business-blue"] 覆盖块
  // 靠这个属性生效，不设置属性时 :root 的默认值（即"默认"皮肤）生效。
  useEffect(() => {
    document.documentElement.setAttribute('data-skin', skin)
  }, [skin])

  const value = useMemo<SkinContextValue>(
    () => ({
      skin,
      setSkin: (next: SkinId) => {
        localStorage.setItem(SKIN_STORAGE_KEY, next)
        setSkinState(next)
      },
    }),
    [skin],
  )

  return <SkinContext.Provider value={value}>{children}</SkinContext.Provider>
}

export function useAdminSkin(): SkinContextValue {
  const value = useContext(SkinContext)
  if (value === null) {
    throw new Error('useAdminSkin() 必须在 <SkinProvider> 内部使用')
  }
  return value
}
