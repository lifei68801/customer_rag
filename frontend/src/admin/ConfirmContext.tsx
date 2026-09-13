import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'

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

  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  //: 弹窗打开前焦点在哪。关闭时还回去。
  const returnFocusRef = useRef<HTMLElement | null>(null)

  /**
   * 焦点管理。WAI-ARIA 的 alertdialog 要求三件事，此前一件都没做：
   *
   * 1. **打开时把焦点移进来**。不移的话焦点还停在遮罩后面那个「删除」按钮
   *    上，读屏软件也不会播报弹窗内容——用户听不到自己正要确认什么。
   * 2. **Tab 不许走出去**。走出去就进到被遮罩盖住的控件里，用户在看不见的
   *    东西之间 Tab，而屏幕上的弹窗还开着。
   * 3. **关闭时把焦点还回去**。不还的话焦点落到 body，下一次 Tab 从页面
   *    顶部重新开始。
   *
   * 落焦点落在**取消**上，不是确认。这是一个破坏性操作的弹窗，确认那颗是
   * 危险的那颗——焦点默认停在它上面，等于邀请用户顺手回车。
   */
  useEffect(() => {
    if (!pending) {
      // 关闭：还焦点。元素可能已经不在 DOM 里（比如刚被删掉的那一行），
      // 所以要先确认它还连着。
      const target = returnFocusRef.current
      returnFocusRef.current = null
      if (target && target.isConnected) target.focus()
      return
    }
    returnFocusRef.current = document.activeElement as HTMLElement | null
    cancelRef.current?.focus()
  }, [pending])

  useEffect(() => {
    if (!pending) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        pending.resolve(false)
        setPending(null)
        return
      }
      if (event.key !== 'Tab') return
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      if (!focusable || focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      // 只在两端接住，中间几个交给浏览器自己走——自己实现整套 Tab 顺序
      // 会跟浏览器的规则不一致（比如 shadow DOM、contenteditable）。
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
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
          className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
          onClick={(event) => {
            if (event.target === event.currentTarget) settle(false)
          }}
        >
          <div
            ref={dialogRef}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="confirm-dialog-message"
            className="flex w-full max-w-sm flex-col gap-4 rounded-modal border border-subtle bg-paper p-5"
          >
            {/* whitespace-pre-line：文案里的换行要留着。删实体的确认框是
                「做什么」+ 空行 +「连带代价」两段，挤成一坨的话最该被读到的
                那句就淹在里面了。不带换行的文案不受影响。 */}
            <p id="confirm-dialog-message" className="whitespace-pre-line text-sm text-ink">
              {pending.message}
            </p>
            <div className="flex justify-end gap-2">
              <button
                ref={cancelRef}
                type="button"
                onClick={() => settle(false)}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-card px-4 py-2 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 ${focusRing}`}
              >
                {pending.cancelLabel}
              </button>
              <button
                type="button"
                onClick={() => settle(true)}
                className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 py-2 text-sm font-bold text-white transition active:scale-95 active:opacity-90 ${focusRing}`}
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
