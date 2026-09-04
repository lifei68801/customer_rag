import { adminFetch } from '../admin/adminApi'

export interface SessionSummary {
  session_id: string
  title: string
  created_at: string
  updated_at: string
}

export interface SessionMessage {
  role: string
  content: string
  created_at: string
}

/**
 * 会话接口不再带 tenant_id / user_id：两者都由服务端从会话 Cookie 里取
 * （admin 取 current_tenant_id，user_id 取用户名）。客户端自报身份的那一
 * 版等于让任何人报谁就是谁。
 *
 * 走 adminFetch 而不是裸 fetch，为的是两件它统一处理的事：写方法要带
 * X-CSRF-Token（缺了会被后端 403），以及 401 时清掉本地会话状态——不清
 * 的话页面会一直显示已登录，而每个请求都 401。第二个参数是 adminFetch
 * 留给 21 个旧调用方的占位符，这里没有 token 可传。
 */
export async function fetchSessions(): Promise<SessionSummary[]> {
  const response = await adminFetch('/agent/sessions', '')
  if (!response.ok) {
    throw new Error(`获取会话列表失败：状态码 ${response.status}`)
  }
  const body = (await response.json()) as { sessions: SessionSummary[] }
  return body.sessions
}

export async function fetchSessionMessages(sessionId: string): Promise<SessionMessage[]> {
  const response = await adminFetch(
    `/agent/sessions/${encodeURIComponent(sessionId)}/messages`,
    '',
  )
  if (!response.ok) {
    throw new Error(`获取会话历史失败：状态码 ${response.status}`)
  }
  const body = (await response.json()) as { messages: SessionMessage[] }
  return body.messages
}

export async function deleteSessionRequest(sessionId: string): Promise<void> {
  const response = await adminFetch(`/agent/sessions/${encodeURIComponent(sessionId)}`, '', {
    method: 'DELETE',
  })
  if (!response.ok) {
    throw new Error(`删除会话失败：状态码 ${response.status}`)
  }
}
