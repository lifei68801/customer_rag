import type { ChatMessage } from '../hooks/useAgentChat'
import { MarkdownContent } from './MarkdownContent'
import { SourceCitations } from './SourceCitations'

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  // 用户的话是气泡，助手的回答是正文。
  //
  // 助手回答经常是带标题、列表、表格、代码块的长 Markdown——塞进一个 75%
  // 宽的描边气泡里，表格要横向滚、代码要折行，而那圈边框还在跟内容自己的
  // 结构抢视觉。让它按全宽正文流排，读起来才是一份答案而不是一条消息。
  //
  // 出错的回答仍然要一眼认出来，所以保留一条左侧的危险色标记——不靠颜色
  // 单独表意，下面 MarkdownContent 之外还有文字说明。
  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={
          isUser
            ? 'max-w-[75%] rounded-card border border-subtle bg-accent-primary px-4 py-3 text-on-accent'
            : message.isError
              ? 'w-full border-l-2 border-status-error py-1 pl-3 text-ink'
              : 'w-full text-ink'
        }
      >
        {/* 流式期间把前几轮已经说过的话留在屏幕上（灰色），不是收进折叠的
            <details> 里。此前每次工具调用都会把气泡清空，观感是"冒一句 →
            擦掉 → 正在查询 → 再冒一句 → 再擦掉"；内容反复消失会让人觉得
            比实际更慢，而且已经读到一半的句子被抽走。答案落定后再收进
            折叠区，正文不被历史轮次挤占。 */}
        {!isUser && message.isStreaming && message.reasoningTrail.length > 0 && (
          <div data-testid="reasoning-live" className="flex flex-col gap-1 pb-1 text-sm text-ink-soft">
            {message.reasoningTrail.map((step, index) => (
              <p key={index} className="whitespace-pre-wrap break-words">
                {step}
              </p>
            ))}
          </div>
        )}
        {message.text ? (
          isUser ? (
            <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>
          ) : (
            <MarkdownContent text={message.text} />
          )
        ) : null}
        {/* 指示器改到文字**下面**，而且有文字时也显示：流式期间它是"还在写"
            的唯一信号，此前只在没有文字时出现，于是一旦开始出字就消失，
            用户分不出"写完了"还是"卡住了"。 */}
        {!isUser && message.isStreaming && (
          <ThinkingIndicator statusText={message.statusText} />
        )}
        {!isUser && !message.isStreaming && message.reasoningTrail.length > 0 && (
          <ReasoningTrail steps={message.reasoningTrail} />
        )}
        {!isUser && !message.isStreaming && message.usedSources.length > 0 && (
          <SourceCitations sources={message.usedSources} />
        )}
      </div>
    </div>
  )
}

function ThinkingIndicator({ statusText }: { statusText?: string }) {
  return (
    <div data-testid="thinking-indicator" className="flex items-center gap-2 py-1">
      {statusText && <span className="text-sm text-ink-soft">{statusText}</span>}
      <div className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none [animation-delay:-0.3s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none [animation-delay:-0.15s]" />
        <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft motion-reduce:animate-none" />
      </div>
    </div>
  )
}

function ReasoningTrail({ steps }: { steps: string[] }) {
  return (
    <details className="mt-2 border-t border-subtle pt-2">
      <summary className="cursor-pointer text-xs text-ink-soft select-none">
        查看推理过程（{steps.length}步）
      </summary>
      <ol className="mt-1 space-y-1 text-xs text-ink-soft">
        {steps.map((step, index) => (
          <li key={index} className="whitespace-pre-wrap break-words">
            {index + 1}. {step}
          </li>
        ))}
      </ol>
    </details>
  )
}
