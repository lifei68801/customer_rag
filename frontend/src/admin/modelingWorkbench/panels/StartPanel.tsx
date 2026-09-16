import type { SkillSummary } from '../types'
import { panelClass, primaryButtonClass, secondaryButtonClass } from '../ui'

/**
 * 还没有工作区时的首屏：选一个内置领域模板，或者空白起步。
 *
 * 每个模板要报出"有多少实体类型/关系类型"——只给名字和一句描述的话，用户
 * 没有任何依据在两个模板之间选。
 */
export function StartPanel(props: {
  skills: SkillSummary[]
  busy: boolean
  onStart: (skillName: string | null) => void
}) {
  return (
    <div className="flex flex-col gap-4">
      {props.skills.length === 0 && (
        <p className="text-sm text-ink-soft">还没有内置领域模板，可以先空白起步。</p>
      )}
      {props.skills.map((skill) => (
        <div key={skill.name} className={`${panelClass} flex flex-col gap-2`}>
          <div className="flex items-baseline gap-2">
            <h2 className="font-mono text-base font-semibold text-ink">{skill.display_name}</h2>
            <span className="text-xs text-ink-soft">v{skill.version}</span>
          </div>
          <p className="text-sm text-ink-soft">{skill.description}</p>
          <p className="text-sm text-ink-soft">
            {`包含 ${skill.term_types.length} 个实体类型、${skill.relation_types.length} 个关系类型、${skill.constraints.length} 条约束`}
          </p>
          <button
            type="button"
            className={primaryButtonClass}
            disabled={props.busy}
            onClick={() => props.onStart(skill.name)}
          >
            用这个模板起步
          </button>
        </div>
      ))}
      <div className={`${panelClass} flex flex-col gap-2`}>
        <h2 className="font-mono text-base font-semibold text-ink">空白起步</h2>
        <p className="text-sm text-ink-soft">
          没有合适的模板时从零开始：先传数据表，让列名告诉你这里有哪些概念。
        </p>
        <button
          type="button"
          className={secondaryButtonClass}
          disabled={props.busy}
          onClick={() => props.onStart(null)}
        >
          空白起步
        </button>
      </div>
    </div>
  )
}
