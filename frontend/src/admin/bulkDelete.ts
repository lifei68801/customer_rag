import { adminFetch, extractErrorDetail } from './adminApi'

/**
 * 批量删除的共享件（前端侧）：请求形状、确认框文案、结果文案。
 *
 * 管理后台一共有 8 个删除点，「全选删除」在它们身上是同一个形状：
 *
 * * 两级全选——**本页**（前端手里已经有这一页的 id）和**当前筛选条件下的
 *   全部**（可能是两万条，前端手里没有、也不该有那份 id 列表）。两者在
 *   界面上必须一眼可辨，它们的破坏力差两个数量级；
 * * 「全部」在服务端展开：请求里给的是筛选条件，不是 id 列表；
 * * 部分失败时能删的删掉，删不掉的逐条报出来。
 *
 * 后端对应 app/api/bulk_delete.py。
 */

/** 一条没删成的目标。key 是这个列表里定位一行的键（实体是 node_key）。 */
export interface BulkDeleteFailure {
  key: string
  reason: string
}

/**
 * 批量删除的结果。
 *
 * requested 和 deleted 都要用上：只报 deleted 的话，用户看到「删除了 97 条」
 * 却不知道自己请求的是 100 条——差额正是他要注意的那部分。
 */
export interface BulkDeleteResult {
  requested: number
  deleted: number
  failures: BulkDeleteFailure[]
}

/** 实体列表的筛选条件，跟后端 TermFilters / GET /terms 的 query 参数一一对应。 */
export interface BulkDeleteFilters {
  term_type?: string
  source?: string
  q?: string
}

/**
 * 这次批量删除的目标。两个变体互斥，对应后端两种互斥的请求模式——同时给
 * node_keys 和 filters 后端会报 400 而不是猜，所以这里用联合类型表达，
 * 让「两个都传」在类型上就写不出来。
 */
export type BulkDeleteTarget =
  | { mode: 'keys'; keys: string[] }
  | { mode: 'filters'; filters: BulkDeleteFilters; total: number }

/** 来源筛选值的中文名，跟实体明细页来源下拉里的选项同一套说法。 */
const SOURCE_LABELS: Record<string, string> = {
  manual: '手工',
  etl: '表格导入',
  review: '文档抽取',
  unknown: '未知（历史数据）',
}

/**
 * 把筛选条件念成人话，供确认框用。
 *
 * 确认框里必须写出**筛选条件和真实条数**：只说「确定删除全部吗」的话，
 * 用户没有任何办法分辨自己按下的是「删这个类型的 12 条」还是「删这个租户
 * 的 20017 条」——而这一步之后没有撤销。
 */
export function describeBulkDeleteFilters(filters: BulkDeleteFilters): string {
  const parts: string[] = []
  if (filters.term_type) parts.push(`实体类型「${filters.term_type}」`)
  if (filters.source) parts.push(`来源「${SOURCE_LABELS[filters.source] ?? filters.source}」`)
  if (filters.q?.trim()) parts.push(`搜索「${filters.q.trim()}」`)
  if (parts.length === 0) return NO_FILTERS_LABEL
  return parts.join(' + ')
}

/**
 * 一个筛选条件都没有时的说法。这一档要单独措辞：套进「筛选条件（…）下的
 * 全部」那个模板会念成「筛选条件（没有任何筛选）下的全部」，绕一圈说了句
 * 废话，而这恰好是破坏力最大的那一档，不该是读起来最费劲的那一句。
 */
export const NO_FILTERS_LABEL = '没有任何筛选'

/** 超过这个条数就按「大批量」措辞。几条和几千条不该是同一句话。 */
const HEAVY_THRESHOLD = 100

/**
 * 确认框文案。
 *
 * 不设硬性条数上限——两万条的清理是这个功能存在的理由——但文案随条数变重：
 * 删 3 条和删 4712 条读起来必须不一样，否则用户对确认框的点击会退化成肌肉
 * 记忆，而那正是大批量误删发生的地方。
 */
export function buildBulkDeleteConfirmMessage(
  target: BulkDeleteTarget,
  noun: string,
): string {
  if (target.mode === 'keys') {
    return `确定删除选中的 ${target.keys.length} 条${noun}吗？此操作不可撤销。`
  }
  const count = target.total.toLocaleString()
  const scope = describeBulkDeleteFilters(target.filters)
  const head =
    scope === NO_FILTERS_LABEL
      ? `即将删除这个租户的全部 ${count} 条${noun}（当前没有任何筛选条件）。`
      : `即将删除筛选条件（${scope}）下的全部 ${count} 条${noun}。`
  if (target.total > HEAVY_THRESHOLD) {
    return (
      `${head}这是一次大批量删除，删掉之后无法恢复——` +
      `请先确认上面这个筛选条件就是你要清理的那一批，再按删除。`
    )
  }
  return `${head}此操作不可撤销。`
}

/**
 * 结果摘要。
 *
 * 全成功和部分失败要说成两句不同的话：都说「删除完成」的话，那 3 条没删掉
 * 的就静默消失了——用户以为清干净了，下次才发现它们还在。失败明细由调用方
 * 单独渲染（见 BulkDeleteOutcome），这里只给一行摘要。
 */
export function summarizeBulkDeleteResult(result: BulkDeleteResult, noun: string): string {
  if (result.failures.length === 0) {
    return `已删除 ${result.deleted.toLocaleString()} 条${noun}。`
  }
  return (
    `请求删除 ${result.requested.toLocaleString()} 条${noun}，` +
    `成功 ${result.deleted.toLocaleString()} 条，` +
    `${result.failures.length.toLocaleString()} 条没能删掉：`
  )
}

/**
 * 发一次批量删除请求。
 *
 * endpoint 由各个删除点自己给（实体是 /api/admin/{tenant}/terms/bulk-delete），
 * 请求体的两种模式在这里落成互斥的两个字段——「全部」发的是筛选条件而不是
 * id 列表，前端不许先把两万条拉回来再逐条删。
 */
export async function requestBulkDelete(
  sessionToken: string,
  endpoint: string,
  target: BulkDeleteTarget,
): Promise<BulkDeleteResult> {
  const body =
    target.mode === 'keys' ? { node_keys: target.keys } : { filters: target.filters }
  const response = await adminFetch(endpoint, sessionToken, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(errorBody, '批量删除失败'))
  }
  return (await response.json()) as BulkDeleteResult
}
