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

export async function fetchSessions(
  tenantId: string,
  userId: string,
): Promise<SessionSummary[]> {
  const params = new URLSearchParams({ tenant_id: tenantId, user_id: userId })
  const response = await fetch(`/agent/sessions?${params.toString()}`)
  if (!response.ok) {
    throw new Error(`获取会话列表失败：状态码 ${response.status}`)
  }
  const body = (await response.json()) as { sessions: SessionSummary[] }
  return body.sessions
}

export async function fetchSessionMessages(
  tenantId: string,
  userId: string,
  sessionId: string,
): Promise<SessionMessage[]> {
  const params = new URLSearchParams({ tenant_id: tenantId, user_id: userId })
  const response = await fetch(`/agent/sessions/${encodeURIComponent(sessionId)}/messages?${params.toString()}`)
  if (!response.ok) {
    throw new Error(`获取会话历史失败：状态码 ${response.status}`)
  }
  const body = (await response.json()) as { messages: SessionMessage[] }
  return body.messages
}

export async function deleteSessionRequest(
  tenantId: string,
  userId: string,
  sessionId: string,
): Promise<void> {
  const params = new URLSearchParams({ tenant_id: tenantId, user_id: userId })
  const response = await fetch(
    `/agent/sessions/${encodeURIComponent(sessionId)}?${params.toString()}`,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    throw new Error(`删除会话失败：状态码 ${response.status}`)
  }
}
