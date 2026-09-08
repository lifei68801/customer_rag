import { adminFetch, extractErrorDetail } from './adminApi'
import type { BulkDeleteTarget } from './bulkDelete'

/**
 * 删实体之前的影响面预演。
 *
 * 实体身上挂着的关系边曾经是拒绝删除的理由，那道守卫防的是「词表说不存在
 * 了，但图谱边还在用它」——而这个状态造不出来：删节点走 DETACH DELETE，
 * 边一定跟着走。守卫的真实效果只是把用户锁死：demo 租户里「产品:Beer」挂着
 * 1013 条订单边，实体删不掉、实体类型因为还有实体也删不掉、边又只能一条条删。
 *
 * 现在的规矩是「告知 + 确认」，而告知有个硬要求：**用户在按下确认之前必须
 * 知道会连带删掉多少条边**。这个模块就是那句话的来源——先问一次后端（只读，
 * 不写任何东西），拿到条数和分布，再拼成确认框里的一句人话。
 *
 * 后端对应 POST /api/admin/{tenant}/terms/bulk-delete/preview。
 */

/** 连带删掉的边里，连向某一类实体的有多少条。 */
export interface CounterpartTypeImpact {
  term_type: string
  edge_count: number
}

/**
 * 预演结果。
 *
 * edge_total 是各分项之和，不是某一项——报成最大的那一项的话，用户按下确认
 * 之后会多没掉一批，而他以为自己已经看过全部代价了。
 */
export interface TermDeletePreview {
  term_count: number
  edge_total: number
  by_counterpart_type: CounterpartTypeImpact[]
}

/** 预演和真删共用同一份请求体形状——两边不一致的话，预演报的就不是真删会删的那批。 */
export function termDeletePreviewBody(target: BulkDeleteTarget): unknown {
  return target.mode === 'keys' ? { node_keys: target.keys } : { filters: target.filters }
}

export async function previewTermDelete(
  sessionToken: string,
  tenantId: string,
  target: BulkDeleteTarget,
): Promise<TermDeletePreview> {
  const response = await adminFetch(
    `/api/admin/${encodeURIComponent(tenantId)}/terms/bulk-delete/preview`,
    sessionToken,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(termDeletePreviewBody(target)),
    },
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(extractErrorDetail(body, '算不出这次删除会波及多少关系边'))
  }
  return (await response.json()) as TermDeletePreview
}

/**
 * 把影响面念成确认框里的一句话。一条边都不牵连时返回空串——「同时会删掉
 * 0 条关系边」是句废话，而确认框里每多一句废话，用户就少读一句真话。
 *
 * 点名占比最大的那一类：只报总数的话，用户没法判断这 1013 条是不是他以为的
 * 那批。全部来自同一类时说「全部来自」而不是「其中 X 条来自」——后者读起来
 * 像还有别的类型没列出来。
 */
export function describeTermDeleteImpact(preview: TermDeletePreview): string {
  if (preview.edge_total <= 0) return ''
  const total = preview.edge_total.toLocaleString()
  const subject = preview.term_count === 1 ? '它的' : ''
  const top = preview.by_counterpart_type[0]
  const breakdown =
    top === undefined
      ? ''
      : top.edge_count >= preview.edge_total
        ? `（全部来自${top.term_type}）`
        : `（其中 ${top.edge_count.toLocaleString()} 条来自${top.term_type}）`
  return `同时会删掉${subject} ${total} 条关系边${breakdown}，这些边不会单独保留。`
}
