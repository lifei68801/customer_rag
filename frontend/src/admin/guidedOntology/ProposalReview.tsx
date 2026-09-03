import type { GuidedDecision, Proposal, RoledColumn } from './types'

const card = 'rounded-card border border-subtle bg-card p-4'
const sectionTitle = 'font-mono text-sm font-bold uppercase tracking-wide text-ink-soft'

interface Props {
  roled: RoledColumn[]
  decision: GuidedDecision
  onDecisionChange: (next: GuidedDecision) => void
  proposal: Proposal
}

export function ProposalReview({ roled, decision, onDecisionChange, proposal }: Props) {
  const dimensions = roled.filter((c) => c.role === 'dimension')
  const dateColumns = roled.filter((c) => c.role === 'date')
  const entityNames = proposal.termTypes.map((t) => t.value)
  const rootName = entityNames.find(
    (name) => !Object.prototype.hasOwnProperty.call(decision.parentOf, name),
  )

  const setDecision = (patch: Partial<GuidedDecision>) =>
    onDecisionChange({ ...decision, ...patch })

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-col gap-3">
        <h2 className={sectionTitle}>这几列，你想怎么用</h2>
        {dimensions.map((column) => {
          const name = column.stats.name
          const asEntity = decision.dimensionsAsEntity[name]
          return (
            <div key={name} data-testid={`dimension-${name}`} className={`${card} flex flex-col gap-2`}>
              <div className="flex flex-wrap items-baseline gap-2">
                <code className="font-mono font-bold text-ink">{name}</code>
                {/* 依据必须带具体数字——"这是维度"用户没法推翻，
                    "10000 行里 50 个不同值"可以：他知道自己业务里州就是
                    50 个。 */}
                <span className="text-xs text-ink-soft">{column.reason}</span>
                {column.stats.samples.length > 0 && (
                  <span className="text-xs text-ink-faint">
                    样例：{column.stats.samples.slice(0, 3).join('、')}
                  </span>
                )}
              </div>
              {/* 不问"该是实体还是属性"——那是建模术语，用户答不了。
                  问他会不会问某类问题，他答得了。 */}
              <label className="flex items-start gap-2 text-sm text-ink">
                <input
                  type="radio"
                  name={`dim-${name}`}
                  checked={asEntity}
                  onChange={() =>
                    setDecision({
                      dimensionsAsEntity: { ...decision.dimensionsAsEntity, [name]: true },
                    })
                  }
                />
                <span>
                  <strong>建成实体</strong>——能问「{column.stats.samples[0] ?? '某个值'}
                  下面有哪些」「哪个{name}最多」这类问题
                </span>
              </label>
              <label className="flex items-start gap-2 text-sm text-ink">
                <input
                  type="radio"
                  name={`dim-${name}`}
                  checked={!asEntity}
                  onChange={() =>
                    setDecision({
                      dimensionsAsEntity: { ...decision.dimensionsAsEntity, [name]: false },
                    })
                  }
                />
                <span>
                  <strong>做成属性</strong>——只能作为过滤条件，问不出上面那些
                </span>
              </label>
            </div>
          )
        })}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className={sectionTitle}>它们怎么连起来</h2>
        {proposal.rootIsGuessed && (
          <p role="alert" className={`${card} text-sm text-ink`}>
            这张表里没有一列是「每行一个值」的标识，所以「{rootName}」是猜的。
            如果它不该是中心，请回上一步换一张表。
          </p>
        )}
        {entityNames
          .filter((name) => name !== rootName)
          .map((name) => (
            <div key={name} className={`${card} flex flex-wrap items-center gap-2`}>
              <label htmlFor={`parent-${name}`} className="text-sm font-bold text-ink">
                {name} 挂在
              </label>
              <select
                id={`parent-${name}`}
                value={decision.parentOf[name] ?? rootName ?? ''}
                onChange={(event) =>
                  setDecision({ parentOf: { ...decision.parentOf, [name]: event.target.value } })
                }
                className="rounded-control border border-subtle bg-paper px-2 py-1 text-sm text-ink"
              >
                {/* 排掉自己：自环会让约束表里出现 A-[R]->A，图谱查询会
                    陷进去。 */}
                {entityNames
                  .filter((candidate) => candidate !== name)
                  .map((candidate) => (
                    <option key={candidate} value={candidate}>
                      {candidate}
                    </option>
                  ))}
              </select>
              <span className="text-sm text-ink-soft">下面，关系叫</span>
              {/* 带 datalist：已经用过的关系名要能选。SOLD_BY 在 demo 里
                  用了两次（订单->公司、产品->公司），不给选的话用户第二次
                  会打出 SELL_BY，建出两个同义关系——图谱里同一件事有两种
                  边，查询时漏掉一半而不报错。 */}
              <input
                aria-label={`${name} 的关系名`}
                list="guided-relation-names"
                value={decision.relationNameOf[name] ?? ''}
                onChange={(event) =>
                  setDecision({
                    relationNameOf: {
                      ...decision.relationNameOf,
                      [name]: event.target.value.toUpperCase(),
                    },
                  })
                }
                className="rounded-control border border-subtle bg-paper px-2 py-1 font-mono text-sm text-ink"
              />
            </div>
          ))}
        <datalist id="guided-relation-names">
          {[...new Set(Object.values(decision.relationNameOf))]
            .filter(Boolean)
            .map((relationName) => (
              <option key={relationName} value={relationName} />
            ))}
        </datalist>
      </section>

      {dateColumns.length > 0 && (
        <section data-testid="date-warning" className={`${card} flex flex-col gap-1`}>
          <h2 className={sectionTitle}>日期列的限制</h2>
          {/* 不说的话，用户会以为"上个月的订单"这类问题能答，直到真去问
              才发现不行。 */}
          <p className="text-sm text-ink">
            {dateColumns.map((c) => c.stats.name).join('、')} 会被存成文本。
            系统目前没有日期类型，所以**按时间范围过滤**（「上个月的」「今年以来的」）
            在图谱层做不了，只能精确匹配。
          </p>
        </section>
      )}

      <section data-testid="unused-columns" className={`${card} flex flex-col gap-1`}>
        <h2 className={sectionTitle}>没有用到的列</h2>
        {/* 不显示等于静默丢弃：用户会在三个月后问"为什么查不到内部备注"，
            而那一列从一开始就没被采纳。 */}
        {proposal.unusedColumns.length === 0 ? (
          <p className="text-sm text-ink-soft">这张表的列都用上了。</p>
        ) : (
          <>
            <p className="text-sm text-ink-soft">
              这些列没有进入本体——它们的重复度不足以当分类，也不是数值。
              如果其中有你需要的，回上一步换一张更聚焦的表，或者建完之后去
              「本体结构」页手工加。
            </p>
            <ul className="flex flex-wrap gap-2">
              {proposal.unusedColumns.map((name) => (
                <li
                  key={name}
                  className="rounded-chip border border-subtle bg-paper px-2 py-0.5 font-mono text-xs text-ink-soft"
                >
                  {name}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  )
}
