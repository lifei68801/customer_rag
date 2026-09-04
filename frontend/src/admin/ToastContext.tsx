import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'

type ToastFn = (message: string) => void

const ToastContext = createContext<ToastFn | null>(null)

/**
 * 站点级瞬时反馈——用于替代"插入后不会消失、还会顶开布局"的常驻
 * 确认文字，或者原本完全没有反馈的操作（删除、上传等）。跟 ConfirmContext
 * 一样用 Context + Provider 模式，挂载在 main.tsx 的根节点（前台聊天页和
 * 后台管理共用）。不支持多条堆叠、不支持手动关闭。
 *
 * 主要用于"操作成功"这类确认性反馈；阻断性错误默认留在原地
 * （role="alert"），紧挨着那个失败的控件。唯一的例外是**发起动作的那块界面
 * 自己就消失了**——目前只有账号菜单里的切换租户（TenantContext.tsx）：菜单
 * 点完就关，错误没有"原地"可停，而完全没有反馈比一条会自动消失的反馈更糟
 * （用户只会以为自己没点中）。往这里挪错误之前先确认符合这个条件。
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null)
  const [visible, setVisible] = useState(false)
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showToast = useCallback<ToastFn>((next) => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current)
    if (clearTimerRef.current) clearTimeout(clearTimerRef.current)
    setMessage(next)
    setVisible(true)
    hideTimerRef.current = setTimeout(() => {
      setVisible(false)
      clearTimerRef.current = setTimeout(() => setMessage(null), 150)
    }, 3000)
  }, [])

  useEffect(() => {
    return () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current)
      if (clearTimerRef.current) clearTimeout(clearTimerRef.current)
    }
  }, [])

  return (
    <ToastContext.Provider value={showToast}>
      {children}
      {message && (
        <div
          role="status"
          aria-live="polite"
          className={`pointer-events-none fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-panel border border-subtle bg-ink px-4 py-2 text-sm font-bold text-paper transition-opacity duration-150 motion-reduce:transition-none ${
            visible ? 'opacity-100' : 'opacity-0'
          }`}
        >
          {message}
        </div>
      )}
    </ToastContext.Provider>
  )
}

export function useToast(): ToastFn {
  const value = useContext(ToastContext)
  if (value === null) {
    throw new Error('useToast() 必须在 <ToastProvider> 内部使用')
  }
  return value
}
