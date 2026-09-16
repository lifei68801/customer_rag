import { describe, expect, it } from 'vitest'
import { normalizeAlias } from './aliases'

/**
 * 归一化规则必须跟后端 app/graphrag/ontology_skills.py::normalize_alias 完全
 * 一致：两边算出来的结果不同，就会出现"前端说这列对上了、后端导出的别名
 * 却对不上"这种谁都解释不了的不一致。这几条用例跟后端那份是同一批输入。
 */
describe('normalizeAlias', () => {
  it.each([
    ['JAN', 'jan'],
    ['sku_code', 'skucode'],
    ['Item CD', 'itemcd'],
    ['retail-price', 'retailprice'],
    ['商品编码', '商品编码'],
    ['  JAN  ', 'jan'],
  ])('%s -> %s', (raw, expected) => {
    expect(normalizeAlias(raw)).toBe(expected)
  })
})
