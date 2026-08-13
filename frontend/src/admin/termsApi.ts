import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
}

export interface TermRecord extends GraphTerm {
  term_type: string
  product_line: string
}

export async function fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]> {
  return fetchTerms(sessionToken)
}

export async function fetchTerms(sessionToken: string): Promise<TermRecord[]> {
  const response = await adminFetch('/api/admin/terms', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: TermRecord[] }
  return data.terms
}

export async function createTerm(sessionToken: string, term: TermRecord): Promise<TermRecord> {
  const response = await adminFetch('/api/admin/terms', sessionToken, {
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
  currentStandardName: string,
  term: TermRecord,
): Promise<TermRecord> {
  const response = await adminFetch(
    `/api/admin/terms/${encodeURIComponent(currentStandardName)}`,
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

export async function deleteTerm(sessionToken: string, standardName: string): Promise<void> {
  const response = await adminFetch(
    `/api/admin/terms/${encodeURIComponent(standardName)}`,
    sessionToken,
    { method: 'DELETE' },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '删除术语失败'))
  }
}
