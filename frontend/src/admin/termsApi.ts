import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
  term_type: string
}

export interface TermRecord extends GraphTerm {
  source: string
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
): Promise<TermPage> {
  const sourceParam = source ? `&source=${encodeURIComponent(source)}` : ''
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms?page=${page}&page_size=${pageSize}${sourceParam}`,
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
  term: TermRecord,
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
  currentStandardName: string,
  currentTermType: string,
  term: TermRecord,
): Promise<TermRecord> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(currentStandardName)}` +
      `?term_type=${encodeURIComponent(currentTermType)}`,
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
  standardName: string,
  termType: string,
): Promise<void> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/${encodeURIComponent(standardName)}` +
      `?term_type=${encodeURIComponent(termType)}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除术语失败'))
  }
}
