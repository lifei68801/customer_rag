import { describe, expect, it } from 'vitest'
import { addMissingAsRelation, addMissingAsTerm, constraintKey, projectSkeleton, setReview } from './skeletonEdits'
import type { InterviewState } from './types'

function baseState(): InterviewState {
  return {
    turns: [
      { role: 'assistant', text: '你们主要做什么生意？' },
      { role: 'user', text: '我们是做消费品零售的，主要卖服装和家居' },
    ],
    skeleton: {
      term_types: [
        {
          value: '商品',
          display_name: '商品',
          rationale: '用户说主要卖服装和家居，商品是最小的售卖单位',
          confidence: 'guess',
          from_turn: 1,
          review: 'pending',
          extra_fields: [{ name: 'price', value_type: 'number', label: '价格' }],
          standard_name_value_type: 'string',
        },
        {
          value: '门店',
          display_name: '门店',
          rationale: '零售一定有卖货的地方',
          confidence: 'guess',
          from_turn: 1,
          review: 'pending',
          extra_fields: [],
          standard_name_value_type: 'string',
        },
      ],
      relation_types: [
        {
          relation_type: 'SOLD_AT',
          example_phrase: '某商品在某门店有售',
          description: '',
          rationale: '商品要在某个门店卖',
          confidence: 'guess',
          from_turn: 1,
          review: 'pending',
        },
      ],
      constraints: [
        {
          subject: '商品',
          relation: 'SOLD_AT',
          object: '门店',
          rationale: '商品卖给门店',
          confidence: 'guess',
          from_turn: 1,
          review: 'pending',
        },
      ],
    },
    questions: [],
    done: false,
  }
}

describe('setReview', () => {
  it('改对应实体类型的审阅状态，不改入参', () => {
    const state = baseState()
    const next = setReview(state, 'term', '商品', 'accepted')
    expect(next.skeleton.term_types.find((t) => t.value === '商品')?.review).toBe('accepted')
    // 入参不变
    expect(state.skeleton.term_types.find((t) => t.value === '商品')?.review).toBe('pending')
    // 没碰到的元素原样保留（同一个对象引用，不是"内容相同的新对象"）
    expect(next.skeleton.term_types.find((t) => t.value === '门店')).toBe(
      state.skeleton.term_types.find((t) => t.value === '门店'),
    )
  })

  it('改对应关系类型的审阅状态', () => {
    const state = baseState()
    const next = setReview(state, 'relation', 'SOLD_AT', 'rejected')
    expect(next.skeleton.relation_types[0].review).toBe('rejected')
    expect(state.skeleton.relation_types[0].review).toBe('pending')
  })

  it('改对应约束的审阅状态，key 是 constraintKey', () => {
    const state = baseState()
    const key = constraintKey(state.skeleton.constraints[0])
    const next = setReview(state, 'constraint', key, 'accepted')
    expect(next.skeleton.constraints[0].review).toBe('accepted')
    expect(state.skeleton.constraints[0].review).toBe('pending')
  })

  it('找不到对应元素时原样返回同一个引用', () => {
    const state = baseState()
    expect(setReview(state, 'term', '不存在', 'accepted')).toBe(state)
    expect(setReview(state, 'relation', '不存在', 'accepted')).toBe(state)
    expect(setReview(state, 'constraint', '不存在|不存在|不存在', 'accepted')).toBe(state)
  })
})

describe('addMissingAsTerm', () => {
  it('按问题清单里缺的名字加一条待审实体类型', () => {
    const state = baseState()
    const next = addMissingAsTerm(state, '品类', '哪个品类卖得最好')
    expect(next).not.toBe(state)
    const added = next.skeleton.term_types.find((t) => t.value === '品类')
    expect(added).toEqual({
      value: '品类',
      display_name: '品类',
      rationale: '来自问题：哪个品类卖得最好',
      confidence: 'guess',
      review: 'pending',
      from_turn: state.turns.length - 1,
      extra_fields: [],
      standard_name_value_type: 'string',
    })
  })

  it('重名原样返回，不重复添加', () => {
    const state = baseState()
    expect(addMissingAsTerm(state, '商品', '哪个商品卖得最好')).toBe(state)
  })

  it('空名原样返回', () => {
    const state = baseState()
    expect(addMissingAsTerm(state, '  ', '随便什么问题')).toBe(state)
  })
})

describe('addMissingAsRelation', () => {
  it('加一条待审关系类型', () => {
    const state = baseState()
    const next = addMissingAsRelation(state, 'HAS_CATEGORY', '哪个品类卖得最好')
    expect(next).not.toBe(state)
    const added = next.skeleton.relation_types.find((r) => r.relation_type === 'HAS_CATEGORY')
    expect(added).toEqual({
      relation_type: 'HAS_CATEGORY',
      example_phrase: '',
      description: '',
      rationale: '来自问题：哪个品类卖得最好',
      confidence: 'guess',
      review: 'pending',
      from_turn: state.turns.length - 1,
    })
  })

  it('重名原样返回', () => {
    const state = baseState()
    expect(addMissingAsRelation(state, 'SOLD_AT', '随便什么问题')).toBe(state)
  })
})

describe('projectSkeleton', () => {
  it('只投影 accepted 的实体和关系类型', () => {
    const state = baseState()
    state.skeleton.term_types[0].review = 'accepted' // 商品
    // 门店仍是 pending
    const payload = projectSkeleton(state)
    expect(payload.term_types.map((t) => t.value)).toEqual(['商品'])
    expect(payload.relation_types).toEqual([])
  })

  it('extra_fields 原样带过去', () => {
    const state = baseState()
    state.skeleton.term_types[0].review = 'accepted'
    const payload = projectSkeleton(state)
    expect(payload.term_types[0].extra_fields).toEqual([{ name: 'price', value_type: 'number', label: '价格' }])
  })

  it('约束要主语、关系、宾语三者都 accepted 才带上', () => {
    const state = baseState()
    state.skeleton.term_types[0].review = 'accepted' // 商品
    state.skeleton.term_types[1].review = 'accepted' // 门店
    state.skeleton.relation_types[0].review = 'pending' // 关系没接受
    state.skeleton.constraints[0].review = 'accepted'
    expect(projectSkeleton(state).constraints).toEqual([])

    state.skeleton.relation_types[0].review = 'accepted'
    expect(projectSkeleton(state).constraints).toEqual([
      { subject_term_type: '商品', relation_type: 'SOLD_AT', object_term_type: '门店' },
    ])
  })

  it('约束本身没被接受时不带，即使三个引用都存在且已接受', () => {
    const state = baseState()
    state.skeleton.term_types[0].review = 'accepted'
    state.skeleton.term_types[1].review = 'accepted'
    state.skeleton.relation_types[0].review = 'accepted'
    state.skeleton.constraints[0].review = 'pending'
    expect(projectSkeleton(state).constraints).toEqual([])
  })
})
