import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
  term_type: string
}

export interface TermRecord extends GraphTerm {
  source: string
  // 身份键。列表接口现在会返回它，编辑/删除按它寻址——标准名在同一
  // term_type 下已经允许重复（2026-08-30），按名字寻址不再唯一。
  node_key: string
  // 属性值。写入时这个字段是可选的，语义由后端定义（admin_terms_routes.py
  // 的 TermWriteRequest）：字段缺席=保留原值，传 {} 才是清空。所以只提交
  // 名字和别名的编辑请求可以安全地不带它，不会把属性值抹掉。
  extra_properties?: Record<string, unknown>
}

export async function fetchGraphTerms(sessionToken: string, tenantId: string): Promise<GraphTerm[]> {
  return fetchTerms(sessionToken, tenantId)
}

export async function fetchTerms(sessionToken: string, tenantId: string): Promise<TermRecord[]> {
  const response = await adminFetch(`/api/admin/${encodeURIComponent(tenantId)}/terms`, sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[] }
  return data.terms
}

export interface TermPage {
  terms: TermRecord[]
  total: number
}

export async function fetchTermsPage(
  sessionToken: string,
  tenantId: string,
  page: number,
  pageSize: number,
  source?: string,
  query?: string,
): Promise<TermPage> {
  const sourceParam = source ? `&source=${encodeURIComponent(source)}` : ''
  // 搜索在后端作用于**合并视图**（terms + 人工编辑），所以人工改过展示名的
  // 术语能用界面上看到的新名字搜到。别改成前端过滤——当前页只有 20 条，
  // 前端过滤等于只搜这一页。
  const queryParam = query?.trim() ? `&q=${encodeURIComponent(query.trim())}` : ''
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms?page=${page}&page_size=${pageSize}${sourceParam}${queryParam}`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[]; total: number }
  return { terms: data.terms, total: data.total }
}

export async function createTerm(
  sessionToken: string,
  tenantId: string,
  term: Omit<TermRecord, 'node_key'>,
): Promise<TermRecord> {
  const response = await adminFetch(`/api/admin/${encodeURIComponent(tenantId)}/terms`, sessionToken, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(term),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '新增术语失败'))
  }
  return (await response.json()) as TermRecord
}

export async function updateTerm(
  sessionToken: string,
  tenantId: string,
  nodeKey: string,
  term: TermRecord,
): Promise<TermRecord> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}`,
    sessionToken,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(term),
    },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '更新术语失败'))
  }
  return (await response.json()) as TermRecord
}

export async function deleteTerm(
  sessionToken: string,
  tenantId: string,
  nodeKey: string,
): Promise<void> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除术语失败'))
  }
}
