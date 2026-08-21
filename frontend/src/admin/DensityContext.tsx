import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

const DENSITY_STORAGE_KEY = 'admin_density'

export type DensityId = 'standard' | 'compact'

const VALID_DENSITY_IDS: readonly DensityId[] = ['standard', 'compact']

function isDensityId(value: string | null): value is DensityId {
  return value !== null && (VALID_DENSITY_IDS as readonly string[]).includes(value)
}

interface DensityContextValue {
  density: DensityId
  setDensity: (next: DensityId) => void
}

const DensityContext = createContext<DensityContextValue | null>(null)

/**
 * 列表/表格密度偏好——管理员个人偏好，存 localStorage，架构照抄
 * SkinContext。跟皮肤不同的是：间距是 Tailwind 静态类名，不是能用
 * CSS 变量驱动的颜色，所以各消费组件要自己读 useAdminDensity() 后
 * 二选一 className；同步到 <html data-density> 的这个属性只作为可选
 * 的 CSS hook 保留，不强制要求消费组件用它。只挂在 AdminLayout 里
 * （不像 SkinProvider 要提升到 main.tsx）：密度只影响后台列表，前台
 * 聊天页的会话列表不在这次改造范围内。
 */
export function DensityProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<DensityId>(() => {
    const stored = localStorage.getItem(DENSITY_STORAGE_KEY)
    return isDensityId(stored) ? stored : 'standard'
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-density', density)
  }, [density])

  const value = useMemo<DensityContextValue>(
    () => ({
      density,
      setDensity: (next: DensityId) => {
        localStorage.setItem(DENSITY_STORAGE_KEY, next)
        setDensityState(next)
      },
    }),
    [density],
  )

  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>
}

export function useAdminDensity(): DensityContextValue {
  const value = useContext(DensityContext)
  if (value === null) {
    throw new Error('useAdminDensity() 必须在 <DensityProvider> 内部使用')
  }
  return value
}
