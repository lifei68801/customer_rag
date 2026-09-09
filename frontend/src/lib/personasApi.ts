import { adminFetch, extractErrorDetail } from '../admin/adminApi'

/**
 * 数字人 = 一个租户 = 一整套本体 + 一整张图。
 *
 * 这不是新概念：前台「还没选租户」的空态里那句话原文就是「租户就是知识库」。
 * 右栏做的事是把已经存在的租户切换器从左下角账号块里的下拉框，升级成常驻的、
 * 看得见的一栏。
 *
 * 后端对应 GET /api/admin/personas——注意不是 /api/admin/tenants，
 * 那个挂在 require_admin_role 上，member 调不了。
 */
export interface Persona {
  tenant_id: string
  name: string
  /** 没配过脸时是空串。前端负责给一个占位，不是显示空白。 */
  avatar: string
  tagline: string
}

export interface PersonaListResponse {
  personas: Persona[]
  current_tenant_id: string | null
}

export async function fetchPersonas(sessionToken: string): Promise<PersonaListResponse> {
  const response = await adminFetch('/api/admin/personas', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '数字人列表加载失败'))
  }
  return (await response.json()) as PersonaListResponse
}

/**
 * 单个数字人的详情，比列表多一个 `questions`。
 *
 * 引导问题只在详情里给，列表里没有：右栏有 N 个数字人，自动生成的引导
 * 问题每算一条都要探一次图，放进列表就是 N 份这样的开销。前台只对**当前**
 * 这一个数字人拉详情，切换时重拉，付的是一个数字人的份而不是 N 个的。
 *
 * 一个数字人的份本身不是一次图查询：后端对每个已确认的关系组合各探一次，
 * 串行（见 app/graphrag/guided_questions.py）。这里省掉的是 N 倍，不是全部。
 */
export type PersonaSource = 'handwritten' | 'generated' | 'unavailable'

export interface PersonaDetail extends Persona {
  questions: string[]
  /**
   * 这批问题是手写的、自动兜底的，还是「本该自动兜底但图谱不通」。
   *
   * 前台不消费它——终端用户看到的都是同一个空引导区，那是诚实的。
   * 后台编辑页消费：管理员是唯一分得清也修得了「没数据」和「图谱坏了」
   * 的人（见 admin/PersonaEditorPage.tsx）。
   */
  questions_source: PersonaSource
}

export async function fetchPersonaDetail(
  sessionToken: string,
  tenantId: string,
): Promise<PersonaDetail> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/persona`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '数字人信息加载失败'))
  }
  return (await response.json()) as PersonaDetail
}
