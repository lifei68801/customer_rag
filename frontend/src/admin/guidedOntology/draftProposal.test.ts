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
      isWholeNumber: false,
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

describe('标识列可以被改判成属性', () => {
  // 一列 10000 行 / 9800 个不同值的「金额分」和一列真订单号在分布上无法
  // 区分，columnRoles 只能猜。猜错时唯一站得住的兜底是用户能改判——所以
  // 标识列必须和维度列走同一张决策表。
  function moneyFirstColumns(): RoledColumn[] {
    return [
      makeColumn('金额分', 'identifier', 9800), // 其实是以分为单位的金额
      makeColumn('产品', 'dimension', 10),
      makeColumn('revenue', 'measure', 500),
    ]
  }

  it('默认仍然建成实体——改判入口不改变默认判定', () => {
    const roled = moneyFirstColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    expect(proposal.termTypes.map((t) => t.value)).toContain('金额分')
  })

  it('改判成属性后不再是实体类型，而是挂在中心上的属性', () => {
    const roled = moneyFirstColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['金额分'] = false
    const proposal = buildProposal(roled, decision)
    expect(proposal.termTypes.map((t) => t.value)).toEqual(['产品'])
    // 不能扔进未使用清单——那等于吞掉用户刚做的决定。
    expect(proposal.unusedColumns).not.toContain('金额分')
    expect(proposal.attributeColumns).toContain('金额分')
  })

  it('改判掉唯一的标识列之后，中心是猜的——告警要出来', () => {
    // 中心现在是「产品」这个维度列。rootIsGuessed 只看"有没有标识列存在过"
    // 的话，这里会答 false，界面上一条提示都不出。
    const roled = moneyFirstColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['金额分'] = false
    const proposal = buildProposal(roled, decision)
    expect(proposal.rootName).toBe('产品')
    expect(proposal.rootIsGuessed).toBe(true)
  })

  it('无小数数值的标识列改判后存成 integer，不是 string', () => {
    // 它落到属性这条路上的典型情形就是"其实是以分为单位的金额"。存成
    // string 的话，聚合和范围过滤在数据层就做不了了。
    const money = makeColumn('金额分', 'identifier', 9800)
    money.stats.isWholeNumber = true
    const roled = [money, makeColumn('产品', 'dimension', 10)]
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['金额分'] = false
    const proposal = buildProposal(roled, decision)
    const host = proposal.termTypes.find((t) => t.value === '产品')
    expect(host?.extra_fields).toHaveLength(1)
    expect(host?.extra_fields[0].value_type).toBe('integer')
  })

  it('真编号（带字母）改判后存成 string', () => {
    // isWholeNumber 为假的标识列，存成 integer 会在 ETL 层炸掉。
    const roled = [makeColumn('订单号', 'identifier', 9998), makeColumn('产品', 'dimension', 10)]
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['订单号'] = false
    const proposal = buildProposal(roled, decision)
    const host = proposal.termTypes.find((t) => t.value === '产品')
    expect(host?.extra_fields[0].value_type).toBe('string')
  })

  it('decision 里没有标识列这一条时，仍然建成实体', () => {
    // buildProposal 也会拿到别处构造的 decision。缺键按 false 处理会把标识
    // 列静默踢出本体——那比误判更糟。
    const roled = moneyFirstColumns()
    const proposal = buildProposal(roled, {
      dimensionsAsEntity: { 产品: true },
      parentOf: {},
      relationNameOf: {},
    })
    expect(proposal.termTypes.map((t) => t.value)).toContain('金额分')
  })
})

describe('会成为属性的列要能被界面点名', () => {
  it('attributeColumns 按原列名列出度量列和日期列', () => {
    const roled = demoColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    expect(proposal.attributeColumns).toContain('revenue')
    expect(proposal.attributeColumns).toContain('purchase_date')
    // 自由文本列不在其中——它进的是未使用清单。
    expect(proposal.attributeColumns).not.toContain('internal_note')
    // 实体列也不在其中。
    expect(proposal.attributeColumns).not.toContain('订单号')
  })
})

describe('表头重名', () => {
  // 「任何一列最终只能是『进了某个实体的 extra_fields』或『进了
  // unusedColumns』」——buildProposal 里明写着不允许第三种下场。按名字判断
  // 「这一列是不是已经成了实体」时这条不变量在重名路径上不成立：第二个
  // 「备注」既不进 extra_fields 也不进 unusedColumns，界面上没有任何异常，
  // 那一列凭空消失。
  function duplicateHeaderColumns(): RoledColumn[] {
    return [
      makeColumn('订单号', 'identifier', 9998),
      makeColumn('备注', 'dimension', 4),
      makeColumn('备注', 'measure', 500),
    ]
  }

  it('第二个同名列成为属性，不会凭空消失', () => {
    const roled = duplicateHeaderColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    const host = proposal.termTypes.find((t) => t.value === '订单号')
    // 中心（订单号）身上要有那一列的属性。按名字判断的实现这里是 0 个。
    expect(host?.extra_fields).toHaveLength(1)
  })

  it('重名的第一列照常成为实体，且只成为一个实体', () => {
    // 实体类型名必须唯一，重名不能变出两个同名实体。
    const roled = duplicateHeaderColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    expect(proposal.termTypes.map((t) => t.value)).toEqual(['订单号', '备注'])
  })

  it('每一列都有下场：要么是实体，要么进 extra_fields，要么进 unusedColumns', () => {
    // 直接把不变量写成断言：三列，三个下场，一个都不许少。
    const roled = duplicateHeaderColumns()
    const proposal = buildProposal(roled, initialDecision(roled))
    const attributeCount = proposal.termTypes.reduce((sum, t) => sum + t.extra_fields.length, 0)
    expect(proposal.termTypes.length + attributeCount + proposal.unusedColumns.length).toBe(
      roled.length,
    )
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

describe('字段名撞车', () => {
  // sanitizeFieldName 的出口很窄：非法字符一律换成下划线。两个不同的中文
  // 列名（a订单 / a客户）会双双清成 a__。后端不查 extra_fields 内部重名，
  // 而 ETL 的 fieldMappings 是 Object.fromEntries——同名映射折叠成一条，
  // 后一列的数据永远不会被加载，界面上没有任何异常。
  function collidingColumns(): RoledColumn[] {
    return [
      makeColumn('订单号', 'identifier', 9998),
      makeColumn('a订单', 'dimension', 10),
      makeColumn('a客户', 'dimension', 10),
    ]
  }

  function collidingDecision() {
    const roled = collidingColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['a订单'] = false
    decision.dimensionsAsEntity['a客户'] = false
    return { roled, decision }
  }

  it('撞名的字段加序号后缀，extra_fields 里没有重名', () => {
    const { roled, decision } = collidingDecision()
    const proposal = buildProposal(roled, decision)
    const names = proposal.termTypes
      .find((t) => t.value === '订单号')!
      .extra_fields.map((f) => f.name)
    expect(names).toHaveLength(2)
    expect(new Set(names).size).toBe(2)
  })

  it('ETL 映射里两列都在，后一列的数据不会消失', () => {
    const { roled, decision } = collidingDecision()
    const { entities } = toEtlBuilder(roled, decision, 'f1')
    const mappings = entities.find((e) => e.termType === '订单号')!.fieldMappings
    // 折叠成一条时这里只有一个键，a订单 那一列永远不会被加载。
    expect(Object.keys(mappings)).toHaveLength(2)
    expect(new Set(Object.values(mappings))).toEqual(new Set(['a订单', 'a客户']))
  })

  it('撞过名的列被点名，界面才说得出发生了什么', () => {
    const { roled, decision } = collidingDecision()
    const proposal = buildProposal(roled, decision)
    expect(proposal.collidedFields).toEqual(['a客户'])
  })

  it('没撞名时 collidedFields 是空的', () => {
    const roled = demoColumns()
    expect(buildProposal(roled, initialDecision(roled)).collidedFields).toEqual([])
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

/**
 * 中心（root）与无效上级。
 *
 * 这一组守的是同一类失败：**界面画出一条边，提交出去却没有**。
 * buildProposal 从前遇到「上级缺失或指向一个已经不在实体列表里的名字」
 * 就 `continue`，那个实体一条边都不会被提交；而 ProposalReview 照样为它
 * 画一行「X 挂在 Y」，用户不点开那个下拉框就永远发现不了。
 */
function threeDimensionColumns(): RoledColumn[] {
  return [
    makeColumn('产品类目', 'dimension', 10), // 猜测根
    makeColumn('品牌', 'dimension', 8),
    makeColumn('颜色', 'dimension', 5),
    makeColumn('revenue', 'measure', 500),
  ]
}

describe('中心实体', () => {
  it('有标识列时，中心就是标识列', () => {
    const roled = demoColumns()
    expect(buildProposal(roled, initialDecision(roled)).rootName).toBe('订单号')
  })

  it('猜测根被改判成属性后，中心顺延给下一个实体——不是变成空的', () => {
    // rootName 为空/undefined 是整类界面故障的源头：审阅视图里实体名渲染
    // 成「」，每个实体都长出「挂在」下拉框，下拉框的值落回第一个选项，
    // 界面于是显示出一个根本不存在的环。
    const roled = threeDimensionColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品类目'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.rootName).toBe('品牌')
    expect(proposal.termTypes.map((t) => t.value)).toEqual(['品牌', '颜色'])
  })

  it('猜测根被改判成属性后，关系不会全部静默消失', () => {
    // 修复前实测：constraints = []、relationTypes = []，而 decision.parentOf
    // 里三个孩子还全都指向那个已经不在实体列表里的旧根。前端没有空关系
    // 校验，这份"零条关系"会被直接 POST 出去，然后显示成功。
    const roled = threeDimensionColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品类目'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.constraints).toEqual([
      { subject_term_type: '品牌', relation_type: 'RELATES_TO', object_term_type: '颜色' },
    ])
    expect(proposal.relationTypes.map((r) => r.relation_type)).toEqual(['RELATES_TO'])
  })

  it('被改挂的实体会被列出来，不是悄悄改的', () => {
    const roled = threeDimensionColumns()
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品类目'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.reparentedTo).toEqual({ root: '品牌', names: ['颜色'] })
  })

  it('上级有效时不会被改挂——改挂只针对无效的上级', () => {
    // 少了这条，一个"所有实体一律改挂到中心"的实现也能让上面几条通过，
    // 而那会把用户在界面上改过的层级悄悄抹平。
    const roled = demoColumns()
    const decision = initialDecision(roled)
    decision.parentOf['类目'] = '产品' // 用户手动改成两层

    const proposal = buildProposal(roled, decision)

    expect(proposal.reparentedTo.names).toEqual([])
    expect(
      proposal.constraints.find((c) => c.object_term_type === '类目')?.subject_term_type,
    ).toBe('产品')
  })

  it('第二个标识列也会拿到一条边，不是静默孤儿', () => {
    // initialDecision 只给维度列写 parentOf，第二个标识列从来就没有条目。
    // 修复前它一条边都拿不到，而审阅视图里那一行的下拉框因为 `?? rootName`
    // 兜底，显示的是「金额 挂在 订单号 下面」——界面显示连好了，提交出去
    // 是孤儿。
    const roled = [
      makeColumn('订单号', 'identifier', 40),
      makeColumn('金额', 'identifier', 40),
      makeColumn('产品', 'dimension', 3),
    ]
    const decision = initialDecision(roled)
    expect(decision.parentOf['金额']).toBeUndefined() // 触发前提

    const proposal = buildProposal(roled, decision)

    expect(
      proposal.constraints.find((c) => c.object_term_type === '金额')?.subject_term_type,
    ).toBe('订单号')
    expect(proposal.reparentedTo.names).toContain('金额')
  })

  it('每个非中心实体恰好一条入边——不多不少', () => {
    // 这条是不变量，UI 依赖它：审阅视图为每个非中心实体画一行「挂在」，
    // 并从 constraints 反查该显示谁。少一条就是界面在说谎，多一条就是同一
    // 个实体挂在两处。
    const roled = [
      makeColumn('订单号', 'identifier', 40),
      makeColumn('金额', 'identifier', 40),
      makeColumn('产品', 'dimension', 3),
      makeColumn('公司', 'dimension', 2),
    ]
    const proposal = buildProposal(roled, initialDecision(roled))
    const nonRoot = proposal.termTypes.map((t) => t.value).filter((v) => v !== proposal.rootName)

    expect(proposal.constraints.map((c) => c.object_term_type).sort()).toEqual(nonRoot.sort())
  })

  it('一个实体都不剩时，中心是空串而不是 undefined', () => {
    const roled: RoledColumn[] = [
      makeColumn('产品', 'dimension', 10),
      makeColumn('revenue', 'measure', 500),
    ]
    const decision = initialDecision(roled)
    decision.dimensionsAsEntity['产品'] = false

    const proposal = buildProposal(roled, decision)

    expect(proposal.rootName).toBe('')
    expect(proposal.constraints).toEqual([])
  })
})
