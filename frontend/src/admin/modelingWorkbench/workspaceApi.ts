import { adminFetch, extractErrorDetail } from '../adminApi'
import type {
  DraftDiff,
  DraftPayload,
  Grounding,
  ModelingWorkspace,
  SkillSummary,
  WorkspaceState,
} from './types'

/**
 * 保存时撞上"期间有别人存过"。单独一个类型，因为页面对它的处置跟别的错误
 * 不一样：这个要提示刷新后重做，别的要提示重试。
 */
export class WorkspaceConflictError extends Error {}

function base(tenantId: string): string {
  return `/api/admin/ontology/${encodeURIComponent(tenantId)}/modeling-workspace`
}

async function readOrThrow(response: Response, fallback: string): Promise<unknown> {
  if (response.ok) return response.json()
  const body = await response.json().catch(() => ({}))
  const detail = extractErrorDetail(body, fallback)
  if (response.status === 409) throw new WorkspaceConflictError(detail)
  throw new Error(detail)
}

export async function fetchSkills(tenantId: string, token: string): Promise<SkillSummary[]> {
  const response = await adminFetch(`${base(tenantId)}/skills`, token)
  const body = (await readOrThrow(response, '读取领域模板失败')) as { skills: SkillSummary[] }
  return body.skills
}

export async function fetchWorkspace(
  tenantId: string,
  token: string,
): Promise<ModelingWorkspace | null> {
  const response = await adminFetch(base(tenantId), token)
  const body = (await readOrThrow(response, '读取建模工作区失败')) as {
    workspace: ModelingWorkspace | null
  }
  return body.workspace
}

export async function createWorkspace(
  tenantId: string,
  token: string,
  skillName: string | null,
): Promise<ModelingWorkspace> {
  const response = await adminFetch(base(tenantId), token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName }),
  })
  const body = (await readOrThrow(response, '创建建模工作区失败')) as {
    workspace: ModelingWorkspace
  }
  return body.workspace
}

export async function saveWorkspace(
  tenantId: string,
  token: string,
  state: WorkspaceState,
  updatedAt: string,
): Promise<ModelingWorkspace> {
  const response = await adminFetch(base(tenantId), token, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state, updated_at: updatedAt }),
  })
  const body = (await readOrThrow(response, '保存建模工作区失败')) as {
    workspace: ModelingWorkspace
  }
  return body.workspace
}

export async function deleteWorkspace(tenantId: string, token: string): Promise<void> {
  const response = await adminFetch(base(tenantId), token, { method: 'DELETE' })
  await readOrThrow(response, '删除建模工作区失败')
}

export async function fetchGrounding(tenantId: string, token: string): Promise<Grounding> {
  const response = await adminFetch(`${base(tenantId)}/grounding`, token)
  return (await readOrThrow(response, '读取落地状态失败')) as Grounding
}

export async function previewApply(
  tenantId: string,
  token: string,
  payload: DraftPayload,
): Promise<DraftDiff> {
  const response = await adminFetch(`${base(tenantId)}/apply-preview`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return (await readOrThrow(response, '计算差异失败')) as DraftDiff
}

export async function exportSkill(
  tenantId: string,
  token: string,
  skillName: string,
  displayName: string,
): Promise<string> {
  const response = await adminFetch(`${base(tenantId)}/export-skill`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill_name: skillName, display_name: displayName }),
  })
  const body = (await readOrThrow(response, '导出领域模板失败')) as { yaml: string }
  return body.yaml
}
