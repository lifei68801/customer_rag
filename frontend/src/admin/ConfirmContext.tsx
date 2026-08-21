import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface ConfirmOptions {
  message: string
  confirmLabel?: string
  cancelLabel?: string
}

type ConfirmFn = (options: ConfirmOptions | string) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

interface PendingConfirm {
  message: string
  confirmLabel: string
  cancelLabel: string
  resolve: (value: boolean) => void
}

/**
 * 应用内确认弹窗，替代 window.confirm()——原生浏览器弹窗没法套用这个项目
 * 自己的粗边框/硬阴影视觉风格，跟整体界面脱节。用法：
 * `if (!(await confirm('确定要删除吗？此操作不可撤销。'))) return`，
 * 跟 window.confirm() 原来的调用方式几乎一样，只是多了个 await。
 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<PendingConfirm | null>(null)

  const confirm = useCallback<ConfirmFn>((options) => {
    const normalized = typeof options === 'string' ? { message: options } : options
    return new Promise<boolean>((resolve) => {
      setPending({
        message: normalized.message,
        confirmLabel: normalized.confirmLabel ?? '确认',
        cancelLabel: normalized.cancelLabel ?? '取消',
        resolve,
      })
    })
  }, [])

  useEffect(() => {
    if (!pending) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        pending.resolve(false)
        setPending(null)
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [pending])

  const settle = (value: boolean) => {
    pending?.resolve(value)
    setPending(null)
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <div
          className="fixed inset-0 z-30 flex items-center justify-center bg-ink/40 p-4"
          onClick={(event) => {
            if (event.target === event.currentTarget) settle(false)
          }}
        >
          <div
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-dialog-message"
            className="flex w-full max-w-sm flex-col gap-4 rounded-modal border border-subtle bg-paper p-5 shadow-soft"
          >
            <p id="confirm-dialog-message" className="text-sm text-ink">
              {pending.message}
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => settle(false)}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-card px-4 py-2 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
              >
                {pending.cancelLabel}
              </button>
              <button
                type="button"
                onClick={() => settle(true)}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 py-2 text-sm font-bold text-white shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
              >
                {pending.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): ConfirmFn {
  const value = useContext(ConfirmContext)
  if (value === null) {
    throw new Error('useConfirm() 必须在 <ConfirmProvider> 内部使用')
  }
  return value
}
