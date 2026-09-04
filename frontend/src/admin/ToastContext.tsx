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
 * （role="alert"），紧挨着那个失败的控件。
 *
 * 例外只有两类，两类的共同点都是**没有"原地"可停**：
 *
 * 1. 发起动作的那块界面自己就消失了——账号菜单里的切换租户
 *    （TenantContext.tsx）：菜单点完就关，错误没有控件可依附，而完全没有
 *    反馈比一条会自动消失的反馈更糟（用户只会以为自己没点中）。
 * 2. 压根没有人发起过动作，是系统自己改了状态——useTenants.ts 里的
 *    「当前租户不存在或已停用，自动换一个有效的」。这不是错误，是一条
 *    **告知**：用户什么都没点，数据作用域却变了，不说的话他只能从账号块
 *    按钮上的租户名发现，而那正是这个项目的头号反模式。它也没有"原地"
 *    ——纠正发生在页面渲染之前，屏幕上没有任何一个控件跟它对应。
 *
 * 第 2 类里，「系统不知道该去哪」的那一路**不属于**这里：那种情况系统不该
 * 替用户决定，正确做法是把选择摆到屏幕上（AdminLayout 的「请先选择一个
 * 租户」空态），而不是先猜一个再用 toast 说一声。toast 会自动消失，用它
 * 承载"你必须做一个决定"是把决定也一起消掉了。
 *
 * 往这里挪东西之前先确认落在上面两类之一。
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
