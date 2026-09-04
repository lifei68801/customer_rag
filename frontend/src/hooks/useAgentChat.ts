import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { adminFetch } from '../admin/adminApi'
import { parseSSEStream } from '../lib/sse'
import {
  deleteSessionRequest,
  fetchSessionMessages,
  fetchSessions,
  type SessionSummary,
} from '../lib/sessionsApi'

const SESSION_QUERY_KEY = 'session'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  usedSources: string[]
  isStreaming: boolean
  isError?: boolean
  statusText?: string
  reasoningTrail: string[]
}

interface SessionChatState {
  messages: ChatMessage[]
  isSending: boolean
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

interface AgentToolStatusEvent {
  type: 'tool_status'
  text: string
}

type AgentEvent = AgentDeltaEvent | AgentFinalEvent | AgentToolStatusEvent | { type: string }

function createId(): string {
  return crypto.randomUUID()
}

const EMPTY_SESSION_STATE: SessionChatState = { messages: [], isSending: false }

/**
 * 会话状态按 session_id 分开存（sessionsData），不是单一一份 messages——
 * 左边栏切换会话时，之前那个会话如果还在流式生成，不会被中断，切回去还能
 * 看到跑完的结果（切换只是换了"当前显示哪个会话"，不影响其它会话的请求
 * 生命周期）。当前会话由 URL 的 ?session= 参数决定，刷新页面/多标签页
 * 打开同一个链接都落在同一个会话上。
 */
export function useAgentChat() {
  const [searchParams, setSearchParams] = useSearchParams()
  const activeSessionId = searchParams.get(SESSION_QUERY_KEY)

  const [sessionsData, setSessionsData] = useState<Record<string, SessionChatState>>({})
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [sessionsError, setSessionsError] = useState<string | null>(null)
  // 已经加载过历史（或者是本地刚创建、根本不需要加载）的 session_id 集合，
  // 用 ref 而不是从 sessionsData 派生——避免"加载历史"这个 effect 依赖
  // sessionsData 本身，形成没必要的重复请求。
  const loadedSessionIdsRef = useRef<Set<string>>(new Set())
  // 组件卸载（比如跳去 /admin）时中止所有还在飞行中的请求，避免残留的
  // setState 打在已卸载组件上；不是用来支持"切换会话时中止"——那个场景
  // 现在是有意让它继续跑。
  const activeControllersRef = useRef<Set<AbortController>>(new Set())

  const refreshSessions = useCallback(async () => {
    try {
      const list = await fetchSessions()
      setSessions(list)
      setSessionsError(null)
    } catch (error) {
      setSessionsError(error instanceof Error ? error.message : '获取会话列表失败')
    }
  }, [])

  useEffect(() => {
    refreshSessions()
  }, [refreshSessions])

  useEffect(() => {
    if (!activeSessionId || loadedSessionIdsRef.current.has(activeSessionId)) return
    loadedSessionIdsRef.current.add(activeSessionId)
    let cancelled = false
    fetchSessionMessages(activeSessionId)
      .then((turns) => {
        if (cancelled) return
        setSessionsData((prev) => ({
          ...prev,
          [activeSessionId]: {
            messages: turns.map((turn) => ({
              id: createId(),
              role: turn.role === 'assistant' ? 'assistant' : 'user',
              text: turn.content,
              usedSources: [],
              isStreaming: false,
              reasoningTrail: [],
            })),
            isSending: false,
          },
        }))
      })
      .catch((error) => {
        // 历史加载失败就当空会话处理——用户仍然可以在这个 session_id 下
        // 继续提问，不能因为一次网络抖动就把整个页面卡死。
        console.error('加载会话历史失败', error)
      })
    return () => {
      cancelled = true
    }
  }, [activeSessionId])

  useEffect(() => {
    return () => {
      for (const controller of activeControllersRef.current) {
        controller.abort()
      }
    }
  }, [])

  const sendQuestion = useCallback(
    async (question: string) => {
      let targetSessionId = activeSessionId
      if (!targetSessionId) {
        targetSessionId = createId()
        loadedSessionIdsRef.current.add(targetSessionId)
        setSearchParams({ [SESSION_QUERY_KEY]: targetSessionId })
      }
      const sessionId = targetSessionId

      const userMessage: ChatMessage = {
        id: createId(),
        role: 'user',
        text: question,
        usedSources: [],
        isStreaming: false,
        reasoningTrail: [],
      }
      const assistantMessageId = createId()
      const assistantMessage: ChatMessage = {
        id: assistantMessageId,
        role: 'assistant',
        text: '',
        usedSources: [],
        isStreaming: true,
        reasoningTrail: [],
      }

      setSessionsData((prev) => {
        const existing = prev[sessionId]?.messages ?? []
        return {
          ...prev,
          [sessionId]: {
            messages: [...existing, userMessage, assistantMessage],
            isSending: true,
          },
        }
      })

      const controller = new AbortController()
      activeControllersRef.current.add(controller)

      const patchAssistantMessage = (patch: Partial<ChatMessage> | ((m: ChatMessage) => ChatMessage)) => {
        setSessionsData((prev) => {
          const session = prev[sessionId]
          if (!session) return prev
          return {
            ...prev,
            [sessionId]: {
              ...session,
              messages: session.messages.map((message) =>
                message.id === assistantMessageId
                  ? typeof patch === 'function'
                    ? patch(message)
                    : { ...message, ...patch }
                  : message,
              ),
            },
          }
        })
      }

      try {
        // 走 adminFetch：POST 要带 X-CSRF-Token（缺了后端 403），401 时它
        // 会清掉本地会话状态，前台的登录门随即把人送回登录页。返回的仍是
        // 原始 Response，SSE 流照常读。
        const response = await adminFetch('/agent/chat', '', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question,
            session_id: sessionId,
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
            patchAssistantMessage((message) => ({
              ...message,
              text: message.text + delta.text,
              statusText: undefined,
            }))
          } else if (parsed.type === 'tool_status') {
            const status = parsed as AgentToolStatusEvent
            // 工具调用轮之前可能出现的前置说明文字（比如"让我查一下。"）
            // 不是最终答案的一部分——tool_status 事件到达就说明这一轮
            // 结束、要开始/继续执行工具了，把已经显示的文字挪进
            // reasoningTrail（供用户按需展开查看推理过程），再清空、
            // 露出"正在查询"指示器，而不是让这段文字停留在气泡里、
            // 之后又被 final 事件悄悄覆盖掉，或者直接永久丢失。
            patchAssistantMessage((message) => ({
              ...message,
              text: '',
              statusText: status.text,
              reasoningTrail: message.text
                ? [...message.reasoningTrail, message.text]
                : message.reasoningTrail,
            }))
          } else if (parsed.type === 'final') {
            const final = parsed as AgentFinalEvent
            patchAssistantMessage({
              text: final.text,
              usedSources: final.used_sources ?? [],
              isStreaming: false,
              statusText: undefined,
            })
          }
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        const detail = error instanceof Error ? error.message : '未知错误'
        patchAssistantMessage((message) => ({
          ...message,
          text: message.text + (message.text ? '\n\n' : '') + `连接后端失败：${detail}，请确认服务已启动。`,
          isStreaming: false,
          isError: true,
          statusText: undefined,
        }))
      } finally {
        activeControllersRef.current.delete(controller)
        setSessionsData((prev) => {
          const session = prev[sessionId]
          if (!session) return prev
          return { ...prev, [sessionId]: { ...session, isSending: false } }
        })
        refreshSessions()
      }
    },
    [activeSessionId, setSearchParams, refreshSessions],
  )

  const selectSession = useCallback(
    (sessionId: string) => {
      setSearchParams({ [SESSION_QUERY_KEY]: sessionId })
    },
    [setSearchParams],
  )

  const startNewSession = useCallback(() => {
    setSearchParams({})
  }, [setSearchParams])

  const deleteSession = useCallback(
    async (sessionId: string) => {
      await deleteSessionRequest(sessionId)
      setSessions((prev) => prev.filter((session) => session.session_id !== sessionId))
      setSessionsData((prev) => {
        const next = { ...prev }
        delete next[sessionId]
        return next
      })
      loadedSessionIdsRef.current.delete(sessionId)
      if (activeSessionId === sessionId) {
        setSearchParams({})
      }
    },
    [activeSessionId, setSearchParams],
  )

  const activeState = activeSessionId ? sessionsData[activeSessionId] ?? EMPTY_SESSION_STATE : EMPTY_SESSION_STATE

  return {
    messages: activeState.messages,
    isSending: activeState.isSending,
    sendQuestion,
    resetConversation: startNewSession,
    sessions,
    sessionsError,
    activeSessionId,
    selectSession,
    deleteSession,
  }
}
