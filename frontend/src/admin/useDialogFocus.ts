import { useEffect, useRef, type RefObject } from 'react'

/**
 * 模态框的焦点管理。WAI-ARIA 的 dialog/alertdialog 要求三件事：
 *
 * 1. **打开时把焦点移进来**。不移的话焦点还停在遮罩后面那个触发按钮上，
 *    读屏软件也不会播报弹窗内容——用户听不到自己面前打开了什么。
 * 2. **Tab 不许走出去**。走出去就进到被遮罩盖住的控件里，用户在看不见的
 *    东西之间 Tab，而屏幕上的弹窗还开着。
 * 3. **关闭时把焦点还回去**。不还的话焦点落到 body，下一次 Tab 从页面
 *    顶部重新开始，用户刚才在哪儿全丢了。
 *
 * 抽成 hook 是因为这套逻辑此前只在确认弹窗里有，而「新建实体」那个弹窗
 * （知识图谱审核页）一条都没做——它比确认框更需要，里面有输入框和两步
 * 流程，焦点不进去的话用户得先 Tab 穿过整个页面才够得着。
 *
 * @param open      弹窗开着没有。
 * @param dialogRef 弹窗最外层那个容器（Tab 只在它里面循环）。
 * @param initialFocusRef 打开时焦点落在哪。不给的话落在容器里第一个可聚焦
 *   元素上。**破坏性操作的弹窗应当显式指向「取消」**：焦点默认停在危险的
 *   那颗按钮上，等于邀请用户顺手回车。
 */
export function useDialogFocus(
  open: boolean,
  dialogRef: RefObject<HTMLElement | null>,
  initialFocusRef?: RefObject<HTMLElement | null>,
) {
  //: 弹窗打开前焦点在哪。关闭时还回去。
  const returnFocusRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) {
      // 关闭：还焦点。元素可能已经不在 DOM 里（比如刚被删掉的那一行），
      // 所以要先确认它还连着。
      const target = returnFocusRef.current
      returnFocusRef.current = null
      if (target && target.isConnected) target.focus()
      return
    }
    returnFocusRef.current = document.activeElement as HTMLElement | null
    const initial = initialFocusRef?.current ?? firstFocusable(dialogRef.current)
    initial?.focus()
    // 只在开/关翻转时跑。把 ref 放进依赖没有意义（ref 对象本身恒定），
    // 而把 .current 放进去会在每次渲染都重跑、反复抢焦点。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return
      const focusable = focusableWithin(dialogRef.current)
      if (focusable.length === 0) return
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
}

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusableWithin(root: HTMLElement | null): HTMLElement[] {
  if (!root) return []
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)]
}

function firstFocusable(root: HTMLElement | null): HTMLElement | null {
  return focusableWithin(root)[0] ?? null
}
