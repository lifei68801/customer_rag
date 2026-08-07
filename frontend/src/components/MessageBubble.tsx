import type { ChatMessage } from '../hooks/useAgentChat'
import { SourceCitations } from './SourceCitations'

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] rounded-bubble px-4 py-3 ${
          isUser
            ? 'bg-accent text-white'
            : message.isError
              ? 'border border-status-error/40 bg-surface-card text-status-error'
              : 'border border-surface-border bg-surface-card text-content-primary'
        }`}
      >
        {message.text ? (
          <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>
        ) : message.isStreaming ? (
          <ThinkingIndicator />
        ) : null}
        {!isUser && !message.isStreaming && message.usedSources.length > 0 && (
          <SourceCitations sources={message.usedSources} />
        )}
      </div>
    </div>
  )
}

function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-1 py-1">
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary [animation-delay:-0.3s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary [animation-delay:-0.15s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary" />
    </div>
  )
}
