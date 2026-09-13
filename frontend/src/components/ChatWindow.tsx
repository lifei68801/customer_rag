import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../hooks/useAgentChat'
import { MessageBubble } from './MessageBubble'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/**
 * 空状态里给的示例问题。
 *
 * 给三条、可点击，而不是在灰字里塞一个例子。空白的对话框不说明这个系统
 * 能回答什么——用户要么不知道从哪问起，要么问一个它根本答不了的问题然后
 * 认定它不好用。示例同时承担两件事：降低启动成本，和划出能力边界。
 *
 * 写成能点的，是因为"照着抄一遍"这一步本身就是摩擦。
 */
const SUGGESTIONS = [
  '网关超时示例是什么意思？',
  '上个月的订单有多少？',
  '这个实体和哪些实体有关系？',
]

interface ChatWindowProps {
  messages: ChatMessage[]
  /** 点了示例问题就直接问出去。不给的话示例只渲染成文字。 */
  onPickSuggestion?: (question: string) => void
}

export function ChatWindow({ messages, onPickSuggestion }: ChatWindowProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    bottomRef.current?.scrollIntoView({ behavior: prefersReducedMotion ? 'auto' : 'smooth' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-5 bg-paper px-4 py-10 text-center">
        <div className="flex flex-col gap-1.5">
          <p className="text-lg font-medium text-ink">有什么想问的？</p>
          <p className="text-sm text-ink-soft">
            我会在你的知识图谱里查一遍再回答，答案下面会标出用到了哪些来源。
          </p>
        </div>
        <ul className="flex w-full max-w-xl flex-col gap-2">
          {SUGGESTIONS.map((question) => (
            <li key={question}>
              <button
                type="button"
                onClick={() => onPickSuggestion?.(question)}
                disabled={!onPickSuggestion}
                className={`w-full cursor-pointer rounded-control border border-subtle bg-card px-4 py-2.5 text-left text-sm text-ink transition hover:bg-interactive-hover disabled:cursor-default disabled:hover:bg-card ${focusRing}`}
              >
                {question}
              </button>
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-y-auto bg-paper px-4 py-6">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
