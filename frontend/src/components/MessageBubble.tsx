import type { ChatMessage } from '../hooks/useAgentChat'
import { MarkdownContent } from './MarkdownContent'
import { SourceCitations } from './SourceCitations'

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] rounded-card border px-4 py-3 shadow-soft ${
          isUser
            ? 'border-subtle bg-accent-pink text-on-accent'
            : message.isError
              ? 'border-status-error bg-card text-ink'
              : 'border-subtle bg-card text-ink'
        }`}
      >
        {message.text ? (
          isUser ? (
            <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>
          ) : (
            <MarkdownContent text={message.text} />
          )
        ) : message.isStreaming ? (
          <ThinkingIndicator statusText={message.statusText} />
        ) : null}
        {!isUser && message.reasoningTrail.length > 0 && (
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
    <div className="flex items-center gap-2 py-1">
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
          <li key={index} className="whitespace-pre-wrap">
            {index + 1}. {step}
          </li>
        ))}
      </ol>
    </details>
  )
}
