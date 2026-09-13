import type { BulkDeleteTarget } from '../bulkDelete'
import type { RelationType, TermType } from '../ontologyTypes'

/**
 * 本体结构页三个 tab 共用的常量和小工具。
 *
 * 单独成文件是因为三个 tab 拆出去之后它们变成了跨文件共享——留在
 * OntologySchemaPage 里的话，三个 tab 都要反向 import 回页面文件，形成
 * 页面↔tab 的循环依赖。
 */

export const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/**
 * 这一页的批量删除只有"选中的这些"一档。
 *
 * 三张表都没有分页也没有筛选，所以"选中的"和"全部"是同一件事——再给一档
 * 「改为选中全部」就是给同一件事配两个按钮。
 */
export const BULK_SCOPE = 'list'

/** 取出这次批量删除的那些 key。'filters' 档在这个页面造不出来（见 BULK_SCOPE）。 */
export function bulkKeys(target: BulkDeleteTarget): string[] {
  return target.mode === 'keys' ? target.keys : []
}

/**
 * 这两张表是后端 app/graphrag/value_types.py 那张权威表的前端副本。
 *
 * 后端在 2026-09-11 把散落七处的类型枚举合并成了一张表，但浏览器读不到
 * Python——加一个类型仍然要在这里同步改一次，而漏改不会有任何信号：下拉框
 * 里少一个选项，没有人会因此变红。
 *
 * 这条重复是已知的、有代价的：要消掉它，得让后端把这张表作为接口暴露出来
 * （比如挂在本体 schema 的某个端点上），前端拉取而不是硬编码。那是一次
 * 接口改动，不在这次拆文件的范围里。
 */
export const VALUE_TYPES = ['string', 'number', 'integer', 'number[]', 'date'] as const

/** 比上面那张窄：standard_name 是实体的名字，一个日期不该当实体的名字。 */
export const STANDARD_NAME_VALUE_TYPES = ['string', 'number', 'integer'] as const

export const emptyTermTypeDraft = (): TermType => ({
  value: '',
  extra_fields: [],
  standard_name_value_type: 'string',
})

export const emptyRelationTypeDraft = (): RelationType => ({
  relation_type: '',
  example_phrase: '',
  description: '',
  allow_chain_query: false,
})
