import { adminFetch, extractErrorDetail } from '../../adminApi'
import type {
  DraftDiff,
  DraftPayload,
  InterviewQuestion,
  InterviewSession,
  InterviewState,
  TurnReport,
} from './types'

/**
 * 访谈接口撞上"期间有别人存过"。单独一个类型，跟 `WorkspaceConflictError`
 * 同样的理由：页面对它的处置（提示刷新重做）跟别的错误（提示重试）不一样。
 * 故意不 import 那一个——三种构建方式各自独立（spec 决策 2）。
 */
export class InterviewConflictError extends Error {
  constructor(message: string) {
    super(message)
    // 显式设置 name：继承 Error 后默认 name 仍是 'Error'。
    this.name = 'InterviewConflictError'
  }
}

function base(tenantId: string): string {
  return `/api/admin/ontology/${encodeURIComponent(tenantId)}/interview`
}

async function readOrThrow(response: Response, fallback: string): Promise<unknown> {
  if (response.ok) return response.json()
  const body = await response.json().catch(() => ({}))
  const detail = extractErrorDetail(body, fallback)
  if (response.status === 409) throw new InterviewConflictError(detail)
  throw new Error(detail)
}

export async function fetchInterview(
  tenantId: string,
  token: string,
): Promise<InterviewSession | null> {
  const response = await adminFetch(base(tenantId), token)
  const body = (await readOrThrow(response, '读取访谈失败')) as { session: InterviewSession | null }
  return body.session
}

export async function startInterview(tenantId: string, token: string): Promise<InterviewSession> {
  const response = await adminFetch(base(tenantId), token, { method: 'POST' })
  const body = (await readOrThrow(response, '开始访谈失败')) as { session: InterviewSession }
  return body.session
}

export async function answerInterview(
  tenantId: string,
  token: string,
  answer: string,
  updatedAt: string,
): Promise<{ session: InterviewSession; turn: TurnReport }> {
  const response = await adminFetch(`${base(tenantId)}/answer`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answer, updated_at: updatedAt }),
  })
  return (await readOrThrow(response, '提交回答失败')) as { session: InterviewSession; turn: TurnReport }
}

export async function saveInterview(
  tenantId: string,
  token: string,
  state: InterviewState,
  updatedAt: string,
): Promise<InterviewSession> {
  const response = await adminFetch(base(tenantId), token, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state, updated_at: updatedAt }),
  })
  const body = (await readOrThrow(response, '保存访谈失败')) as { session: InterviewSession }
  return body.session
}

export async function deleteInterview(tenantId: string, token: string): Promise<void> {
  const response = await adminFetch(base(tenantId), token, { method: 'DELETE' })
  await readOrThrow(response, '删除访谈失败')
}

export async function addQuestion(
  tenantId: string,
  token: string,
  text: string,
  updatedAt: string,
): Promise<{ session: InterviewSession; question: InterviewQuestion }> {
  const response = await adminFetch(`${base(tenantId)}/questions`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, updated_at: updatedAt }),
  })
  return (await readOrThrow(response, '添加业务问题失败')) as {
    session: InterviewSession
    question: InterviewQuestion
  }
}

/**
 * 写入草稿前的差异预览。复用既有 `apply-preview` 端点（跟模板构建同一个
 * 后端路由），但在这里自己再封一次——不 import `modelingWorkbench/workspaceApi`
 * 的 `previewApply`（spec 决策 2：三种构建方式各自独立）。
 */
export async function previewDraft(
  tenantId: string,
  token: string,
  payload: DraftPayload,
): Promise<DraftDiff> {
  const response = await adminFetch(
    `/api/admin/ontology/${encodeURIComponent(tenantId)}/modeling-workspace/apply-preview`,
    token,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  )
  return (await readOrThrow(response, '计算差异失败')) as DraftDiff
}
