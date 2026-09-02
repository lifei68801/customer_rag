import { describe, expect, it } from 'vitest'
import { buildProposal, initialDecision, suggestRelationName, toEtlBuilder } from './draftProposal'
import type { RoledColumn } from './types'

/** demo 租户那张电商订单宽表，本项目里真实存在的形状。 */
function demoColumns(): RoledColumn[] {
  const col = (name: string, role: RoledColumn['role'], distinctCount: number): RoledColumn => ({
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
  })
  return [
    col('订单号', 'identifier', 9998),
    col('产品', 'dimension', 10),
    col('公司', 'dimension', 3),
    col('类目', 'dimension', 4),
    col('用户名', 'dimension', 800),
    col('revenue', 'measure', 500),
    col('units_sold', 'measure', 20),
    col('purchase_date', 'date', 300),
    col('customer_state', 'dimension', 50),
    col('internal_note', 'freetext', 6000),
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
    for (const relation of proposal.relationTypes) {
      expect(relation.allow_chain_query).toBe(true)
    }
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
})
