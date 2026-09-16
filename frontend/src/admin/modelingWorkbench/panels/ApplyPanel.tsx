import type { DraftDiff } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass } from '../ui'

/**
 * 应用：先看 diff，再写草稿。
 *
 * 删除项单独醒目列出（spec 决策 10）：replace_draft 是整份替换，草稿里用户
 * 在「本体结构」页手工加的东西会被这次替换删掉，不说出来就是静默删数据。
 */
export function ApplyPanel(props: {
  diff: DraftDiff | null
  busy: boolean
  onPreview: () => void
  onApply: () => void
  onExport: () => void
}) {
  return (
    <section className={`${panelClass} flex flex-col gap-3`}>
      <h2 className="font-mono text-base font-semibold text-ink">应用到本体草稿</h2>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={secondaryButtonClass} disabled={props.busy} onClick={props.onPreview}>
          看看会改什么
        </button>
        <button type="button" className={primaryButtonClass} disabled={props.busy} onClick={props.onApply}>
          写入草稿
        </button>
        <button type="button" className={secondaryButtonClass} disabled={props.busy} onClick={props.onExport}>
          导出为领域模板
        </button>
      </div>
      {props.diff === null ? (
        <p className="text-sm text-ink-soft">
          先点「看看会改什么」：整份替换会把草稿换成工作区里已接受的那些，草稿里别处加的东西会被删掉。
        </p>
      ) : (
        <div className="flex flex-col gap-1 text-sm text-ink">
          {props.diff.removed_term_types.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉：${props.diff.removed_term_types.join('、')}（多半是你在「本体结构」页手工加的）`}
            </p>
          )}
          {props.diff.removed_relation_types.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉关系类型：${props.diff.removed_relation_types.join('、')}`}
            </p>
          )}
          {props.diff.added_term_types.length > 0 && (
            <p>{`会新增：${props.diff.added_term_types.join('、')}`}</p>
          )}
          {props.diff.added_relation_types.length > 0 && (
            <p>{`会新增关系类型：${props.diff.added_relation_types.join('、')}`}</p>
          )}
          {props.diff.changed_term_types.map((line) => (
            <p key={line}>{`会改：${line}`}</p>
          ))}
          {props.diff.added_constraints.length > 0 && (
            <p>{`会新增约束：${props.diff.added_constraints.join('、')}`}</p>
          )}
          {props.diff.removed_constraints.length > 0 && (
            <p role="alert" className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink">
              {`会删掉约束：${props.diff.removed_constraints.join('、')}`}
            </p>
          )}
          {Object.values(props.diff).every((items) => items.length === 0) && (
            <p className="text-ink-soft">没有差异，草稿已经是这个样子。</p>
          )}
        </div>
      )}
    </section>
  )
}
