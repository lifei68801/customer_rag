import { describe, expect, it } from 'vitest'
import { buildOntologyDiff } from './ontologyDiff'

describe('确认差异的数据影响', () => {
  it('删除还有实体在用的类型时说清会失效多少条', () => {
    // "旧的已确认版本会被换掉、无法恢复"这句话用户理解成"本体定义会被换掉"
    // ——没错，但不完整。真正的后果在数据侧，而那一半此前没说。
    const diff = buildOntologyDiff(
      { termTypes: [], relationTypes: [], constraints: [] },
      { termTypes: [{ value: '客户', extra_fields: [] }], relationTypes: [], constraints: [] },
      { 客户: 9335 },
    )
    const removed = diff.termTypes.find((r) => r.kind === 'removed')
    expect(removed?.impact).toMatch(/9335/)
  })

  it('没有实体在用的类型不加噪音', () => {
    const diff = buildOntologyDiff(
      { termTypes: [], relationTypes: [], constraints: [] },
      { termTypes: [{ value: '空类型', extra_fields: [] }], relationTypes: [], constraints: [] },
      {},
    )
    expect(diff.termTypes.find((r) => r.kind === 'removed')?.impact).toBeUndefined()
  })

  it('新增类型不带数据影响——没有东西会失效', () => {
    const diff = buildOntologyDiff(
      { termTypes: [{ value: '新类型', extra_fields: [] }], relationTypes: [], constraints: [] },
      { termTypes: [], relationTypes: [], constraints: [] },
      { 新类型: 5 },
    )
    expect(diff.termTypes.find((r) => r.kind === 'added')?.impact).toBeUndefined()
  })
})
