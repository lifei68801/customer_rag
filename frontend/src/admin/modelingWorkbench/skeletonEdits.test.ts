import { describe, expect, it } from 'vitest'
import { addManualClue, addManualTermType, renameTermType } from './skeletonEdits'
import type { WorkspaceState, WorkspaceTermType } from './types'

const term = (value: string): WorkspaceTermType => ({
  value,
  display_name: value,
  provenance: 'skill',
  review: 'accepted',
  standard_name_value_type: 'string',
  extra_fields: [],
  key_aliases: ['jan'],
  field_aliases: {},
  clues: [],
  data_match: null,
})

const state = (): WorkspaceState => ({
  term_types: [term('SKU')],
  relation_types: [],
  constraints: [
    { subject: 'SKU', relation: 'SOLD_AT', object: 'Store', provenance: 'skill', review: 'pending' },
  ],
  sources: [],
  unmatched_columns: {},
  questions: [],
})

describe('renameTermType', () => {
  it('改名时把约束里的引用一起改掉', () => {
    // 不一起改的话，投影时约束引用的类型不存在，整条会被悄悄丢掉
    const next = renameTermType(state(), 'SKU', '商品')
    expect(next.term_types[0].value).toBe('商品')
    expect(next.constraints[0].subject).toBe('商品')
  })

  it('别名保留：改的是名字，不是"这个概念怎么从列名认出来"', () => {
    expect(renameTermType(state(), 'SKU', '商品').term_types[0].key_aliases).toEqual(['jan'])
  })

  it('改成已存在的名字时原样返回（调用方负责提示）', () => {
    const before = state()
    before.term_types.push(term('Store'))
    expect(renameTermType(before, 'SKU', 'Store')).toBe(before)
  })

  it('空名字不接受', () => {
    const before = state()
    expect(renameTermType(before, 'SKU', '   ')).toBe(before)
  })

  it('不改入参', () => {
    const before = state()
    const snapshot = JSON.stringify(before)
    renameTermType(before, 'SKU', '商品')
    expect(JSON.stringify(before)).toBe(snapshot)
  })
})

describe('addManualClue', () => {
  it('旁证追加到对应实体上，带上是谁什么时候加的', () => {
    const next = addManualClue(state(), 'SKU', '数据下个月接', 'alice', '2026-09-16T10:00:00')
    expect(next.term_types[0].clues).toEqual([
      { kind: 'manual', note: '数据下个月接', by: 'alice', at: '2026-09-16T10:00:00' },
    ])
  })

  it('空旁证不加', () => {
    const before = state()
    expect(addManualClue(before, 'SKU', '  ', 'alice', 'now')).toBe(before)
  })
})

describe('addManualTermType', () => {
  it('手工新增的元素来源是 manual、状态直接是 accepted', () => {
    // 用户自己敲进去的，不需要再自己审一遍
    const next = addManualTermType(state(), '促销活动')
    const added = next.term_types.find((t) => t.value === '促销活动')!
    expect(added.provenance).toBe('manual')
    expect(added.review).toBe('accepted')
    expect(added.key_aliases).toEqual(['促销活动'])
  })

  it('重名时原样返回', () => {
    const before = state()
    expect(addManualTermType(before, 'SKU')).toBe(before)
  })
})
