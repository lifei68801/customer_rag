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
  // 没有它的话，"重新开始对话"可以在上一轮流式回复还没结束时点击：旧请求
  // 不会被取消，它的 finally 仍会在之后的某个时刻把 isSending 设回
  // false——如果那时新一轮请求正在进行中，输入框就会被错误地提前解锁。
  const abortControllerRef = useRef<AbortController | null>(null)

  const resetConversation = useCallback(() => {
    abortControllerRef.current?.abort()
    setMessages([])
    sessionIdRef.current = createId()
  }, [])

  const sendQuestion = useCallback(async (question: string) => {
    abortControllerRef.current?.abort()
    const controller = new AbortController()
    abortControllerRef.current = controller

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
        signal: controller.signal,
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
      // 主动 abort（重置对话/发下一条消息）不算错误，不应该往气泡里追加
      // "连接失败"文案——这条消息本来就要被丢弃或已经被新一轮请求取代。
      if (error instanceof DOMException && error.name === 'AbortError') {
        return
      }
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
      // 只有当前这一轮请求仍然是"活跃"的那一个才允许它解锁输入框——
      // 被 abort 的旧请求不能覆盖新一轮请求还在进行中的 loading 状态。
      if (abortControllerRef.current === controller) {
        setIsSending(false)
      }
    }
  }, [])

  return { messages, isSending, sendQuestion, resetConversation }
}
