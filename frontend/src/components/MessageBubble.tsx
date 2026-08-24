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
