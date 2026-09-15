import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { deduplicateHeader } from './sourceParser'

// 跟后端 tests/graphrag/test_etl_staging.py 读的是同一个文件。语言不同没法
// 共享代码，但可以共享判据——这是唯一能真正防住两边去重规则分叉的手段，
// 而不是靠两边各自写注释提醒对方。
const CASES_PATH = resolve(__dirname, '../../../../fixtures/header-dedup-cases.json')
const cases = JSON.parse(readFileSync(CASES_PATH, 'utf-8')).cases as {
  name: string
  input: string[]
  expected: string[]
}[]

describe('deduplicateHeader 与后端同源', () => {
  it.each(cases)('$name', ({ input, expected }) => {
    expect(deduplicateHeader(input)).toEqual(expected)
  })

  it('用例数跟后端的下限一致', () => {
    // fixture 被清空时 it.each 会一条都不跑，测试文件依然"全绿"。
    expect(cases.length).toBeGreaterThanOrEqual(8)
  })
})
