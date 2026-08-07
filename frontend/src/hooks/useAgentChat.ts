import { useCallback, useRef, useState } from 'react'
import { parseSSEStream } from '../lib/sse'

const TENANT_ID = 'demo'
const USER_ID = 'demo-user'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  usedSources: string[]
  isStreaming: boolean
  isError?: boolean
}

interface AgentDeltaEvent {
  type: 'delta'
  text: string
}

interface AgentFinalEvent {
  type: 'final'
  text: string
  used_sources: string[]
  audio_segments_base64: string[] | null
}

type AgentEvent = AgentDeltaEvent | AgentFinalEvent | { type: string }

function createId(): string {
  return crypto.randomUUID()
}

export function useAgentChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isSending, setIsSending] = useState(false)
  const sessionIdRef = useRef<string>(createId())

  const sendQuestion = useCallback(async (question: string) => {
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      text: question,
      usedSources: [],
      isStreaming: false,
    }
    const assistantMessageId = createId()
    const assistantMessage: ChatMessage = {
      id: assistantMessageId,
      role: 'assistant',
      text: '',
      usedSources: [],
      isStreaming: true,
    }
    setMessages((prev) => [...prev, userMessage, assistantMessage])
    setIsSending(true)

    try {
      const response = await fetch('/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          tenant_id: TENANT_ID,
          session_id: sessionIdRef.current,
          user_id: USER_ID,
          voice_response: false,
        }),
      })

      if (!response.ok) {
        throw new Error(`后端返回状态码 ${response.status}`)
      }

      for await (const event of parseSSEStream(response)) {
        const parsed = JSON.parse(event.data) as AgentEvent

        if (parsed.type === 'delta') {
          const delta = parsed as AgentDeltaEvent
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantMessageId
                ? { ...message, text: message.text + delta.text }
                : message,
            ),
          )
        } else if (parsed.type === 'final') {
          const final = parsed as AgentFinalEvent
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantMessageId
                ? {
                    ...message,
                    text: final.text,
                    usedSources: final.used_sources ?? [],
                    isStreaming: false,
                  }
                : message,
            ),
          )
        }
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : '未知错误'
      setMessages((prev) =>
        prev.map((message) =>
          message.id === assistantMessageId
            ? {
                ...message,
                text:
                  message.text +
                  (message.text ? '\n\n' : '') +
                  `连接后端失败：${detail}，请确认服务已启动。`,
                isStreaming: false,
                isError: true,
              }
            : message,
        ),
      )
    } finally {
      setIsSending(false)
    }
  }, [])

  return { messages, isSending, sendQuestion }
}
