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

export interface TermTypeGroup {
  term_type: string
  total: number
}

/** 按实体类型分组的条数，实体列表的分组摘要用。 */
export async function fetchTermsSummary(
  sessionToken: string,
  tenantId: string,
): Promise<TermTypeGroup[]> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/summary`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载分组失败'))
  }
  return ((await response.json()) as { groups: TermTypeGroup[] }).groups
}

export async function fetchTermsPage(
  sessionToken: string,
  tenantId: string,
  page: number,
  pageSize: number,
  source?: string,
  query?: string,
  termType?: string,
): Promise<TermPage> {
  const sourceParam = source ? `&source=${encodeURIComponent(source)}` : ''
  const typeParam = termType ? `&term_type=${encodeURIComponent(termType)}` : ''
  // 搜索在后端作用于**合并视图**（terms + 人工编辑），所以人工改过展示名的
  // 术语能用界面上看到的新名字搜到。别改成前端过滤——当前页只有 20 条，
  // 前端过滤等于只搜这一页。
  const queryParam = query?.trim() ? `&q=${encodeURIComponent(query.trim())}` : ''
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms?page=${page}&page_size=${pageSize}${sourceParam}${queryParam}${typeParam}`,
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

/**
 * 删掉这个实体参与的一条关系边。
 *
 * 边按业务键定位（这个实体 + 方向 + 关系类型 + 对端 node_key），不是按
 * Neo4j 内部 id——那个 id 重建库之后就变了。direction 跟详情页展示这条边
 * 时用的是同一个字段，界面上看到的方向就是发出去的方向。
 */
export async function deleteTermRelation(
  sessionToken: string,
  tenantId: string,
  nodeKey: string,
  relation: { direction: 'in' | 'out'; relationType: string; otherNodeKey: string },
): Promise<void> {
  const query = new URLSearchParams({
    direction: relation.direction,
    relation_type: relation.relationType,
    other_node_key: relation.otherNodeKey,
  })
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}/relations?${query}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除关系失败'))
  }
}

/** 一条租户标记异常的关系边（后端 InconsistentTermRelation 的镜像）。 */
export interface InconsistentTermRelation {
  direction: 'in' | 'out'
  relation_type: string
  // 跨租户的边上，对端属于另一个租户：只有平台管理员看得到它的身份，
  // 其他人拿到的是 null。
  node_key: string | null
  standard_name: string | null
  term_type: string | null
  other_tenant_id: string | null
  edge_tenant_id: string | null
  category: 'edge_tenant_mismatch' | 'cross_tenant'
  deletable: boolean
}

/**
 * 这个实体身上租户标记异常的关系边。
 *
 * 它们在正常那份关系清单里一条都不出现（两边都按边的 tenant_id 过滤），
 * 却照样挡着实体的删除——这是它们唯一的入口。
 */
export async function fetchInconsistentTermRelations(
  sessionToken: string,
  tenantId: string,
  nodeKey: string,
): Promise<InconsistentTermRelation[]> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}/relations/inconsistent`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '读取异常关系边失败'))
  }
  const body = (await response.json()) as { inconsistent_relations?: InconsistentTermRelation[] }
  return body.inconsistent_relations ?? []
}

/**
 * 删掉一条租户标记异常的关系边。
 *
 * 比正常那条多传一个 other_tenant_id：这类边定位靠的是两端节点各自的
 * 租户（边自己标的那个值恰恰就是错的）。
 */
export async function deleteInconsistentTermRelation(
  sessionToken: string,
  tenantId: string,
  nodeKey: string,
  relation: {
    direction: 'in' | 'out'
    relationType: string
    otherNodeKey: string
    otherTenantId: string
  },
): Promise<void> {
  const query = new URLSearchParams({
    direction: relation.direction,
    relation_type: relation.relationType,
    other_node_key: relation.otherNodeKey,
    other_tenant_id: relation.otherTenantId,
  })
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(nodeKey)}/relations/inconsistent?${query}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除关系失败'))
  }
}
