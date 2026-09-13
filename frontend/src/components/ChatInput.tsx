import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { SendHorizontal } from 'lucide-react'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/** 输入框最高长到这么高就开始内部滚动，不再顶走消息区。 */
const MAX_INPUT_HEIGHT_PX = 200

interface ChatInputProps {
  disabled: boolean
  onSend: (question: string) => void
}

export function ChatInput({ disabled, onSend }: ChatInputProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  //: 输入法是不是正在组词。中文/日文输入时，选字过程中的回车是"确认候选"，
  //: 不是"发送"——不挡住的话，用户打到一半按回车选词，半句话就飞出去了。
  const composingRef = useRef(false)

  // 跟着内容长高。先归零再读 scrollHeight：不归零的话它只会变高不会变矮，
  // 删掉几行之后输入框还占着原来的高度。
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_HEIGHT_PX)}px`
  }, [value])

  const submit = () => {
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setValue('')
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    submit()
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter') return
    // event.nativeEvent.isComposing 是标准信号；composingRef 兜住那些在
    // compositionend 之后才派发 keydown 的输入法（Windows 上的微软拼音
    // 就是这样）。两个都查，漏一个就会吞掉或误发。
    if (composingRef.current || event.nativeEvent.isComposing) return
    if (event.shiftKey) return // Shift+Enter 换行
    event.preventDefault()
    submit()
  }

  const canSend = !disabled && value.trim().length > 0

  return (
    <form
      onSubmit={handleSubmit}
      className="border-t border-subtle bg-card px-4 py-3"
    >
      {/* 输入框和按钮包在同一个描边容器里，看起来是一个控件而不是两个。
          焦点环打在容器上（focus-within），所以 textarea 自己不再画一圈。 */}
      <div
        className={`flex items-end gap-2 rounded-card border border-subtle bg-paper px-3 py-2 transition focus-within:border-accent-primary`}
      >
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => {
            composingRef.current = true
          }}
          onCompositionEnd={() => {
            composingRef.current = false
          }}
          placeholder="输入你的问题…"
          disabled={disabled}
          aria-label="输入你的问题"
          className="max-h-[200px] flex-1 resize-none bg-transparent py-1.5 text-ink placeholder:text-ink-soft focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!canSend}
          aria-label="发送"
          className={`flex min-h-[36px] min-w-[36px] cursor-pointer items-center justify-center rounded-control bg-accent-primary text-on-accent transition active:scale-95 disabled:cursor-not-allowed disabled:opacity-40 ${focusRing}`}
        >
          {/* 图标按钮的名字靠 aria-label 给，不再额外塞一个 sr-only 文本
              ——aria-label 存在时后者根本不会被读，只是多一个 DOM 节点。 */}
          <SendHorizontal aria-hidden="true" className="h-4 w-4" />
        </button>
      </div>
      {/* 快捷键说出来，不让用户靠试。 */}
      <p className="mt-1.5 px-1 text-xs text-ink-soft">
        Enter 发送 · Shift + Enter 换行
      </p>
    </form>
  )
}
