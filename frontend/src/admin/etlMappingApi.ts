import { adminFetch, extractErrorDetail } from './adminApi'

/** 映射会怎么处理一张表——后端从 YAML 解析出来的、给界面看的形状。 */
export interface EtlMappingSummary {
  entities: {
    term_type: string
    source_file: string
    /** 哪些列拼成身份键。稳定码那种展示成"按 X 列分配编号"。 */
    key_columns: string[]
    name_columns: string[]
    /** {字段名: 源列}——挂在这个实体上的属性。 */
    attributes: Record<string, string>
  }[]
  relations: { relation_type: string; subject_term_type: string; object_term_type: string }[]
}

export interface EtlMapping {
  config_yaml: string
  source_file_name: string
  created_at: string
  /** 存下来的 YAML 解析不了时为 null。老接口不带这个字段。 */
  summary?: EtlMappingSummary | null
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
