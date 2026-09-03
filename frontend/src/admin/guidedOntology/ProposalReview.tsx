import type { GuidedDecision, Proposal, RoledColumn } from './types'

const card = 'rounded-card border border-subtle bg-card p-4'
const sectionTitle = 'font-mono text-sm font-bold uppercase tracking-wide text-ink-soft'
/** 「挂在」下拉框在没有对应 constraint 时的哨兵值——不是真实实体名。 */
const UNCONNECTED = '__unconnected__'

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
  // 中心实体读 proposal.rootName，**不再**用「不在 decision.parentOf 里的
  // 实体」反推。反推在用户把猜测根改判成属性后会得到 undefined：那时
  // 下面的 filter 不再排除任何人，每个实体都长出一行「挂在」下拉框，而
  // 下拉框的值（旧根）不在 options 里，DOM 静默回落到第一个选项——界面
  // 显示「品牌挂在颜色下」「颜色挂在品牌下」这样一个环，提交出去却是零条
  // 关系。
  const rootName = proposal.rootName
  // 每行「挂在」显示什么，一律从 proposal.constraints 反查，不从 decision
  // 猜：constraints 就是要提交的东西，从它取值，界面显示的和提交的必然
  // 一致。decision.parentOf 里可能留着指向已消失实体的陈旧条目。
  const parentOfEntity = new Map(
    proposal.constraints.map((c) => [c.object_term_type, c.subject_term_type]),
  )
  const relationOfEntity = new Map(
    proposal.constraints.map((c) => [c.object_term_type, c.relation_type]),
  )
  // 「没有用到的列」小节要显示每一列具体为什么没进本体——一句通用说明
  // 对所有落进这里的列都成立是做不到的（空列、整数金额、真正的自由
  // 文本，原因完全不同），只有各列自己的 reason 才是真的。
  const reasonByColumn = new Map(roled.map((c) => [c.stats.name, c.reason]))

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
        {/* 文案只许承诺这个界面真做得到的动作。
            「回上一步换一张表」做不到：纯维度表本来就没有标识列，换任何
            一张同类的表结果都一样。
            「在下面把实体重新挂到你想要的那一列下面」也换不掉中心：中心
            自己没有「挂在」那一行（下面的 filter 把它排除了），下拉框能做
            的是重挂**非中心**实体。
            真做得到的只有一件事：把中心那一列在上面改判成「做成属性」，
            中心会顺延给列顺序里下一个还是实体的列（buildProposal 的
            rootName 就是这么算的），原来挂在它下面的实体会自动改挂过去。 */}
        {proposal.rootIsGuessed && rootName === '' && (
          // rootName 为空串当且仅当所有维度列都被用户改判成了属性——一个
          // 实体都不剩。旧文案在这条分支下渲染成「现在拿「」当中心，其余
          // 实体都挂在它下面」：中心名是空的、没有其余实体、「改成做成
          // 属性」正是用户刚做完的事，三处都不成立。这条分支专门说清
          // 「本体是空的，没法提交」，并指回用户能做的事——把某一列改回
          // 「建成实体」。
          <p role="alert" data-testid="empty-ontology-warning" className={`${card} text-sm text-ink`}>
            这张表里没有一列是「每行一个值」的标识，而上面的列又都被改成了
            「做成属性」，本体里现在一个实体都没有——没有实体就没法写入
            草稿。把上面至少一列改回「建成实体」。
          </p>
        )}
        {proposal.rootIsGuessed && rootName !== '' && (
          <p role="alert" className={`${card} text-sm text-ink`}>
            这张表里没有一列是「每行一个值」的标识，所以中心是猜的：现在拿「
            {rootName}」当中心，其余实体都挂在它下面。这里换不了中心——如果它
            不该当中心，把它在上面改成「做成属性」，中心会顺延给下一列。
          </p>
        )}
        {proposal.reparentedTo.names.length > 0 && (
          // 不说的话就是界面在说谎：这些实体的上级要么已经不在了（被改判成
          // 属性），要么从来就没指定过（第二个标识列不在 initialDecision 的
          // parentOf 里），而下面照样画出一行「X 挂在 Y」。现在边真的会按这
          // 里说的提交，但改挂这件事必须让用户看见并能推翻。
          <p role="alert" data-testid="reparented-notice" className={`${card} text-sm text-ink`}>
            {proposal.reparentedTo.names.join('、')} 没有指定有效的上级，已经挂到中心「
            {proposal.reparentedTo.root}」下面。不对的话在下面改。
          </p>
        )}
        {proposal.constraints.length === 0 && proposal.termTypes.length > 1 && (
          // 兜底：多个实体、零条关系意味着提交出去是一堆互不相连的孤岛，
          // 图谱里问不出任何跨实体的问题。仍然允许提交（用户可能就是想先
          // 建出实体），但不能让他以为一切正常。
          // 现在的 buildProposal 产不出这个组合（每个非中心实体都会被改挂
          // 到中心下面，拿到一条边），这里守的是渲染边界——Proposal 是外部
          // 传进来的 prop，未来任何让关系归零的改动都会先在这里被看见。
          <p role="alert" data-testid="no-relations-warning" className={`${card} text-sm text-ink`}>
            这份草案里有 {proposal.termTypes.length} 个实体，但它们之间一条关系都没有。
            写进草稿后，跨实体的问题（「某某下面有哪些」）答不出来。
          </p>
        )}
        {entityNames
          .filter((name) => name !== rootName)
          .map((name) => {
            const parent = parentOfEntity.get(name)
            return (
              <div key={name} className={`${card} flex flex-wrap items-center gap-2`}>
                <label htmlFor={`parent-${name}`} className="text-sm font-bold text-ink">
                  {name} 挂在
                </label>
                <select
                  id={`parent-${name}`}
                  value={parent ?? UNCONNECTED}
                  onChange={(event) =>
                    setDecision({ parentOf: { ...decision.parentOf, [name]: event.target.value } })
                  }
                  className="rounded-control border border-subtle bg-paper px-2 py-1 text-sm text-ink"
                >
                  {/* 没有对应的 constraint 时不能悄悄兜底成 rootName——那
                      会画出一条「X 挂在中心」的边，而这条边根本不会被提交
                      （这轮 Critical 的形态）。当前 buildProposal 的不变量
                      保证每个非中心实体都有一条边，这条分支此刻应该走不到；
                      留着它是为了不让将来任何打破那条不变量的改动，重新
                      变成一次静默失败。 */}
                  {parent === undefined && (
                    <option value={UNCONNECTED} disabled>
                      未连接
                    </option>
                  )}
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
                  value={relationOfEntity.get(name) ?? ''}
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
            )
          })}
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

      {Object.keys(proposal.renamedFields).length > 0 && (
        <section data-testid="renamed-fields" className={`${card} flex flex-col gap-1`}>
          <h2 className={sectionTitle}>字段名被清洗过的列</h2>
          {/* 属性名被清洗改过的列必须显示：sanitizeFieldName 对纯中文
              列名会兜底成 field_1 这种占位名，不说的话用户下载 ETL 配置
              后会在 YAML 里看到自己从没在界面上见过的字段名——数据没
              丢，但改动对用户不可见，是"静默失败"的典型形态。 */}
          <p className="text-sm text-ink-soft">
            这些列名不是合法的属性字段名，已经清洗成新的名字——ETL 配置
            里用的是清洗后的名字，不是原始列名。
          </p>
          <ul className="flex flex-col gap-1">
            {Object.entries(proposal.renamedFields).map(([original, cleaned]) => (
              <li key={original} className="font-mono text-xs text-ink-soft">
                {original} → {cleaned}
              </li>
            ))}
          </ul>
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
            {/* 这句是唯一对每一类落进这里的列都成立的说明——「重复度不足以
                当分类，也没高到每行一个」这类具体理由是维度/自由文本列的
                原因，对空列或整数列（比如 3 行整数金额，ratio 恰好是 1.0，
                只是行数不够）是假的。真正的原因见每一列后面那句，来自
                columnRoles.ts 的 reason，不是这里编一句能覆盖所有情况的话。 */}
            <p className="text-sm text-ink-soft">
              这些列没有进入本体，原因见每一列后面的说明。如果其中有你需要的，
              回上一步换一张更聚焦的表，或者建完之后去「本体结构」页手工加。
            </p>
            <ul className="flex flex-col gap-1">
              {proposal.unusedColumns.map((name) => (
                <li key={name} className="flex flex-wrap items-baseline gap-2 text-xs">
                  <code className="rounded-chip border border-subtle bg-paper px-2 py-0.5 font-mono text-ink-soft">
                    {name}
                  </code>
                  <span className="text-ink-soft">{reasonByColumn.get(name)}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
    </div>
  )
}
