import type { DraftDiff, ModelingWorkspace } from './types'

/**
 * 工作台顶部那一行"下一步建议"。
 *
 * 工作台是工作台不是向导（spec 决策 12）——四个面板随时都能点。但"随时都能点"
 * 对第一次来的人等于没有入口，所以用一句话指出当下最该做的事，同时不挡任何
 * 别的操作。
 */
export function nextStepHint(
  workspace: ModelingWorkspace | null,
  diff: DraftDiff | null,
): string {
  if (workspace === null) return '先选一个领域模板起步，或者空白起步。'
  const terms = workspace.state.term_types
  const relations = workspace.state.relation_types
  if (terms.length === 0 && relations.length === 0) {
    return '骨架还是空的——在「骨架」面板手工新增，或者传一张表让数据告诉你有什么。'
  }
  if (terms.some((t) => t.review === 'pending') || relations.some((r) => r.review === 'pending')) {
    return '先去「骨架」面板审阅骨架：每一条接受还是拒绝。拒掉的会留在工作区里，随时能翻回来。'
  }
  const accepted = terms.filter((t) => t.review === 'accepted')
  if (accepted.length === 0) return '骨架里一条都没接受，先去「骨架」面板至少接受一个实体类型。'
  if (accepted.every((t) => t.data_match === null)) {
    return '骨架审完了，去「数据」面板上传数据表，看看哪些概念真有数据。'
  }
  if (diff !== null && Object.values(diff).every((items) => items.length === 0)) {
    return '工作区已经和草稿一致，没有要应用的改动。'
  }
  return '有接受且接上数据的元素了，去「应用」面板看差异，确认后应用到草稿。'
}
