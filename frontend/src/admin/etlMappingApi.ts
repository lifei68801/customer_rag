import { adminFetch, extractErrorDetail } from './adminApi'

export interface EtlMapping {
  config_yaml: string
  source_file_name: string
  created_at: string
}

/** 读挂在本体上的 ETL 映射。表格导入页用它决定首屏形态。 */
export async function fetchEtlMapping(
  sessionToken: string,
  tenantId: string,
  status: 'draft' | 'confirmed',
): Promise<EtlMapping | null> {
  const response = await adminFetch(
    `/api/admin/ontology/${encodeURIComponent(tenantId)}/etl-mapping?status=${status}`,
    sessionToken,
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '加载 ETL 映射失败'))
  }
  return ((await response.json()) as { mapping: EtlMapping | null }).mapping
}
