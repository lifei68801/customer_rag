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
