import { describe, expect, it } from 'vitest'
import {
  buildProposal,
  initialDecision,
  sanitizeFieldName,
  suggestRelationName,
  toEtlBuilder,
} from './draftProposal'
import type { RoledColumn } from './types'

function makeColumn(name: string, role: RoledColumn['role'], distinctCount: number): RoledColumn {
  return {
    stats: {
      name,
      nonEmptyCount: 10000,
      distinctCount,
      distinctCapped: false,
      samples: [],
      inferredType: role === 'measure' ? 'number' : role === 'date' ? 'date' : 'string',
    },
    role,
    reason: '',
  }
}

/** demo 租户那张电商订单宽表，本项目里真实存在的形状。 */
function demoColumns(): RoledColumn[] {
  return [
    makeColumn('订单号', 'identifier', 9998),
    makeColumn('产品', 'dimension', 10),
    makeColumn('公司', 'dimension', 3),
    makeColumn('类目', 'dimension', 4),
    makeColumn('用户名', 'dimension', 800),
    makeColumn('revenue', 'measure', 500),
    makeColumn('units_sold', 'measure', 20),
    makeColumn('purchase_date', 'date', 300),
    makeColumn('customer_state', 'dimension', 50),
    makeColumn('internal_note', 'freetext', 6000),
  ]
}

/**
 * 纯维度表（产品主数据这类），没有标识列——root 是猜的。用来覆盖「猜测根
 * 被用户取消勾选」这条路径。
 */
function noIdentifierColumns(): RoledColumn[] {
  return [
    makeColumn('产品', 'dimension', 10), // 猜测根
    makeColumn('类目', 'dimension', 4),
    makeColumn('revenue', 'measure', 500),
    makeColumn('purchase_date', 'date', 300),
  ]
}

describe('默认决定', () => {
  it('所有维度列默认建成实体类型', () => {
    // 少建是静默错误（那类问题就是答不出来，不报错），多建看得见
    // （实体列表里就有）。所以默认往实体偏。
    const decision = initialDecision(demoColumns())
    for (const name of ['产品', '公司', '类目', '用户名', 'customer_state']) {
      expect(decision.dimensionsAsEntity[name]).toBe(true)
    }
  })

  it('默认是星型：所有实体都挂在标识列下', () => {
    // 星型一定连通，不会漏掉任何实体；多一条冗余边是看得见的，用户一眼
    // 就能说"这条不对"。反过来默认不连、让用户自己加，漏掉的那条不会
    // 有任何提示。
    const decision = initialDecision(demoColumns())
    expect(decision.parentOf['产品']).toBe('订单号')
    expect(decision.parentOf['类目']).toBe('订单号')
  })
})

describe('生成草案', () => {
  it('标识列和被选中的维度都成为实体类型', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const values = proposal.termTypes.map((t) => t.value).sort()
    expect(values).toEqual(
      ['产品', '公司', '类目', '用户名', '订单号', 'customer_state'].sort(),
    )
  })

  it('度量和日期成为标识列的属性', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    const fieldNames = order.extra_fields.map((f) => f.name)
    expect(fieldNames).toContain('revenue')
    expect(fieldNames).toContain('purchase_date')
  })

  it('日期属性存成 string——数据模型没有日期类型', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    const date = order.extra_fields.find((f) => f.name === 'purchase_date')!
    expect(date.value_type).toBe('string')
  })

  it('维度改成属性之后，就不再是实体类型了', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['customer_state'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.termTypes.map((t) => t.value)).not.toContain('customer_state')
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    expect(order.extra_fields.map((f) => f.name)).toContain('customer_state')
  })

  it('改挂之后约束跟着变', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.parentOf['类目'] = '产品'
    decision.relationNameOf['类目'] = 'BELONG_TO'

    const proposal = buildProposal(roled, decision)

    expect(proposal.constraints).toContainEqual({
      subject_term_type: '产品',
      relation_type: 'BELONG_TO',
      object_term_type: '类目',
    })
    expect(proposal.constraints).not.toContainEqual(
      expect.objectContaining({ subject_term_type: '订单号', object_term_type: '类目' }),
    )
  })

  it('自由文本列不进本体，进未使用清单', () => {
    // 不显示的话，用户永远不知道自己丢了什么——他会在三个月后问
    // "为什么查不到内部备注"，而那一列从一开始就没被采纳。
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    expect(proposal.unusedColumns).toContain('internal_note')
  })

  it('每个关系类型只出现一次，哪怕用在多条边上', () => {
    // SOLD_BY 在 demo 里用了两次（订单→公司、产品→公司）。重复声明会撞
    // 主键 (tenant_id, relation_type, status)。
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.relationNameOf['公司'] = 'SOLD_BY'
    decision.parentOf['类目'] = '产品'
    decision.relationNameOf['类目'] = 'SOLD_BY'

    const proposal = buildProposal(roled, decision)

    const names = proposal.relationTypes.map((r) => r.relation_type)
    expect(new Set(names).size).toBe(names.length)
  })

  it('关系类型一律 allow_chain_query', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    // for...of 遍历空数组时循环体一次都不会执行，断言也就形同虚设——
    // 得先证明数组非空，下面的 for...of 才是在真的验证什么。
    expect(proposal.relationTypes.length).toBeGreaterThan(0)
    for (const relation of proposal.relationTypes) {
      expect(relation.allow_chain_query).toBe(true)
    }
  })

  it('有标识列时 rootIsGuessed 是 false', () => {
    const proposal = buildProposal(demoColumns(), initialDecision(demoColumns()))
    expect(proposal.rootIsGuessed).toBe(false)
  })

  it('没有标识列时 rootIsGuessed 是 true，UI 要提示用户确认', () => {
    const roled = noIdentifierColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    expect(proposal.rootIsGuessed).toBe(true)
  })

  it('猜测根被用户取消勾选时，属性顺延挂到剩下的实体上，不会静默消失', () => {
    // 触发路径：没有标识列 -> root 是猜的第一个维度列 -> 用户把它改判
    // 成属性 -> root 自己已经不在 entityNames 里了，度量/日期属性不能
    // 再指望挂在 root 上，得顺延给还留着的实体。
    const roled = noIdentifierColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品'] = false

    const proposal = buildProposal(roled, decision)

    const category = proposal.termTypes.find((t) => t.value === '类目')!
    const fieldNames = category.extra_fields.map((f) => f.name)
    expect(fieldNames).toContain('revenue')
    expect(fieldNames).toContain('purchase_date')
    // 任何一列最终只能是「进了某个实体的 extra_fields」或「进了
    // unusedColumns」，不允许两头都不沾。
    expect(proposal.unusedColumns).not.toContain('revenue')
    expect(proposal.unusedColumns).not.toContain('purchase_date')
  })

  it('实体一个都不剩时，属性列全部落进未使用清单，不会凭空消失', () => {
    const roled: RoledColumn[] = [
      makeColumn('产品', 'dimension', 10),
      makeColumn('revenue', 'measure', 500),
    ]
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.termTypes).toEqual([])
    expect(proposal.unusedColumns).toContain('revenue')
    expect(proposal.unusedColumns).toContain('产品')
  })
})

describe('关系命名建议', () => {
  it('全大写、下划线，符合后端的校验', () => {
    // ^[A-Z][A-Z0-9_]{0,63}$ —— 不合规的名字后端会 400，而用户看不出
    // 是哪个字符的问题。
    const name = suggestRelationName('订单号', '产品')
    expect(name).toMatch(/^[A-Z][A-Z0-9_]{0,63}$/)
  })

  it('中文列名也能产出合规的名字', () => {
    expect(suggestRelationName('订单号', '用户名')).toMatch(/^[A-Z][A-Z0-9_]{0,63}$/)
  })

  it('英文列名产出 HAS_ 前缀、全大写的具体名字', () => {
    // 上面两条中文用例清洗后 ascii 恒为空串，两次都退到 fallback
    // RELATES_TO——而 RELATES_TO 本身也满足正则，所以只验证了 fallback
    // 合规，从没走过 HAS_ 这条主分支。这里断言具体值，不只是"匹配正则"。
    expect(suggestRelationName('order_id', 'customer_state')).toBe('HAS_CUSTOMER_STATE')
  })

  it('超长英文列名截断到 64 字符', () => {
    const longName = 'x'.repeat(70)
    const result = suggestRelationName('order_id', longName)
    expect(result).toBe('HAS_' + 'X'.repeat(60))
    expect(result.length).toBe(64)
  })
})

describe('字段名清洗', () => {
  it('纯中文列名——清洗后没有一个字母数字——退回带序号的占位名', () => {
    // 第一步把每个非法字符换成下划线，本身不会产出空串："客户备注" 会
    // 变成 "____"，而下划线合法，不会被第二步的"去掉不合法前导字符"
    // 清掉。必须判断"有没有至少一个字母/数字"，不能只判断是否空串。
    expect(sanitizeFieldName('客户备注', 0)).toBe('field_1')
  })

  it('非法字符换成下划线，只要还剩字母数字就保留清洗结果', () => {
    expect(sanitizeFieldName('unit price($)', 2)).toBe('unit_price___')
  })

  it('数字开头的列名，去掉前导数字', () => {
    expect(sanitizeFieldName('123abc', 0)).toBe('abc')
  })

  it('纯数字列名退回占位名', () => {
    expect(sanitizeFieldName('123', 0)).toBe('field_1')
  })
})

describe('顺带产出 ETL 映射', () => {
  it('每个实体类型都有对应的映射，属性列一并带上', () => {
    // 引导收集的信息已经够生成映射了。让用户在 ETL 页把同样的判断再做
    // 一遍是重复劳动，而且两次结果可能不一致。
    const roled = demoColumns()
    const { entities } = toEtlBuilder(roled, initialDecision(roled), 'file-1')
    const order = entities.find((e) => e.termType === '订单号')!
    expect(order.standardNameColumn).toBe('订单号')
    expect(order.nodeKeyParts).toEqual([{ kind: 'column', column: '订单号' }])
    expect(order.fieldMappings.revenue).toBe('revenue')
  })

  it('每条边都有对应的关系映射', () => {
    const roled = demoColumns()
    const decision = initialDecision(roled)
    const { relations } = toEtlBuilder(roled, decision, 'file-1')
    expect(relations).toContainEqual(
      expect.objectContaining({ subjectTermType: '订单号', objectTermType: '产品' }),
    )
  })

  it('中文维度列改成属性后，字段名被清洗，ETL 映射能反查回原列名', () => {
    // demoColumns 里所有会成为属性的列本身就是合法英文标识符，清洗前后
    // 名字不变，renamedFields 一直是空对象——toEtlBuilder 里
    // `columnOfField.get(field.name) ?? field.name` 的反查分支
    // （?? 左边那个）从来没被触发过。这里故意把中文维度列（类目）改判
    // 成属性，逼它走清洗 + 反查这条路。
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['类目'] = false

    const proposal = buildProposal(roled, decision)
    const order = proposal.termTypes.find((t) => t.value === '订单号')!
    const fieldName = proposal.renamedFields['类目']
    expect(fieldName).toBeDefined()
    expect(order.extra_fields.map((f) => f.name)).toContain(fieldName)

    const { entities } = toEtlBuilder(roled, decision, 'file-1')
    const orderEntity = entities.find((e) => e.termType === '订单号')!
    expect(orderEntity.fieldMappings[fieldName!]).toBe('类目')
  })
})
