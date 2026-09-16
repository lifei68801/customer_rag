import { describe, expect, it } from 'vitest'
import { draftFromOptions, optionsFromDraft, skippedRows } from './parseSettingsDraft'

describe('draftFromOptions', () => {
  it('缺省选项显示成"第 1 行表头、首数据行留空"', () => {
    expect(draftFromOptions({})).toEqual({ sheet: undefined, headerRow: '1', firstDataRow: '' })
  })

  it('存着的选项原样回填到输入框里', () => {
    expect(draftFromOptions({ sheet: 'Master', headerRow: 6, firstDataRow: 7 })).toEqual({
      sheet: 'Master',
      headerRow: '6',
      firstDataRow: '7',
    })
  })
})

describe('optionsFromDraft', () => {
  it('填好的合法值翻译成解析选项', () => {
    expect(optionsFromDraft({ sheet: 'Master', headerRow: '6', firstDataRow: '7' })).toEqual({
      ok: true,
      options: { sheet: 'Master', headerRow: 6, firstDataRow: 7 },
    })
  })

  it('首数据行留空 = 紧跟表头，不是 0', () => {
    expect(optionsFromDraft({ sheet: undefined, headerRow: '6', firstDataRow: '' })).toEqual({
      ok: true,
      options: { headerRow: 6 },
    })
  })

  it('表头行清空时给一句话，不当成第 1 行', () => {
    // 用户清空输入框那一瞬间值就是空字符串。当成 1 的话，他会看到列名突然
    // 跳到标题带那一行，而他并没有做过这个选择。
    const result = optionsFromDraft({ sheet: undefined, headerRow: '', firstDataRow: '' })

    expect(result.ok).toBe(false)
    expect(result.ok === false && result.message).toMatch(/表头行/)
  })

  it('表头行是 0 时给出解析器自己的那句话，不自作主张改成 1', () => {
    // 把 10 改成 2 的中途会出现 0。拒绝规则只有 assertValidParseOptions 一处，
    // 这里把它的说法原样交给用户。
    const result = optionsFromDraft({ sheet: undefined, headerRow: '0', firstDataRow: '' })

    expect(result.ok).toBe(false)
    expect(result.ok === false && result.message).toMatch(/从 1 开始/)
  })

  it('首数据行不大于表头行时拒绝', () => {
    const result = optionsFromDraft({ sheet: undefined, headerRow: '6', firstDataRow: '6' })

    expect(result.ok).toBe(false)
    expect(result.ok === false && result.message).toMatch(/firstDataRow|首数据行/)
  })

  it('不是整数的行号直接拒绝，不按 parseInt 截一半', () => {
    // parseInt('6.9') 是 6，parseInt('6x') 也是 6——都不是用户填的东西。
    expect(optionsFromDraft({ sheet: undefined, headerRow: '6x', firstDataRow: '' }).ok).toBe(false)
  })
})

describe('skippedRows', () => {
  it('首数据行跳过一段时给出被跳过的行号区间', () => {
    expect(skippedRows({ headerRow: 2, firstDataRow: 5 })).toEqual({ from: 3, to: 4 })
  })

  it('紧跟表头时没有跳过的行', () => {
    expect(skippedRows({ headerRow: 2, firstDataRow: 3 })).toBeNull()
    expect(skippedRows({ headerRow: 2 })).toBeNull()
  })
})
