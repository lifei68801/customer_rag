import { adminFetch, extractErrorDetail } from './adminApi'

export interface GraphTerm {
  standard_name: string
  aliases: string[]
}

export async function fetchGraphTerms(sessionToken: string): Promise<GraphTerm[]> {
  const response = await adminFetch('/api/admin/graph-reviews/terms', sessionToken)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载术语表失败'))
  }
  const data = (await response.json()) as { terms: GraphTerm[] }
  return data.terms
}
