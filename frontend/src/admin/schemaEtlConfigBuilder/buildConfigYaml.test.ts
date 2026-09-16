import { describe, expect, it } from 'vitest'
import { buildConfigYaml } from './buildConfigYaml'

describe('buildConfigYaml 的 sources 段', () => {
  it('把每个文件的解析设置写进 sources 段', () => {
    const file = new File([''], 'CN_001_SKU_MASTER_121.xls')
    const yaml = buildConfigYaml({
      tenantId: 'muji',
      entities: [],
      relations: [],
      files: [
        {
          id: 'f1',
          file,
          columns: [],
          parseOptions: { sheet: 'Master', headerRow: 6, firstDataRow: 7 },
        },
      ],
    })

    expect(yaml).toContain('sources:')
    expect(yaml).toContain('  - file: "CN_001_SKU_MASTER_121.xls"')
    expect(yaml).toContain('    sheet: "Master"')
    expect(yaml).toContain('    header_row: 6')
    expect(yaml).toContain('    first_data_row: 7')
  })

  it('没有改过解析设置的文件不写进 sources 段', () => {
    // 全是缺省值还写一遍，等于把"第 1 行"固化进配置。将来缺省变了，这些
    // 配置不会跟着变，而用户从没做过这个选择。
    const file = new File([''], 'plain.csv')
    const yaml = buildConfigYaml({
      tenantId: 'muji',
      entities: [],
      relations: [],
      files: [{ id: 'f1', file, columns: [], parseOptions: {} }],
    })

    expect(yaml).not.toContain('sources:')
  })

  it('sheet 是序号时按数字写，不加引号', () => {
    // 写成 sheet: "1" 的话，后端会把它当成一张名叫 "1" 的工作表去找。
    const file = new File([''], 'a.xls')
    const yaml = buildConfigYaml({
      tenantId: 'muji',
      entities: [],
      relations: [],
      files: [{ id: 'f1', file, columns: [], parseOptions: { sheet: 1 } }],
    })

    expect(yaml).toContain('    sheet: 1')
  })

  it('sources 段排在 entities 之前——后端按 sources 决定怎么读表，entities 才谈映射', () => {
    const yaml = buildConfigYaml({
      tenantId: 'muji',
      entities: [],
      relations: [],
      files: [{ id: 'f1', file: new File([''], 'a.xls'), columns: [], parseOptions: { headerRow: 6 } }],
    })

    // 少了这句的话，sources: 根本没写时 indexOf 返回 -1，下面那条照样通过。
    expect(yaml).toContain('sources:')
    expect(yaml.indexOf('sources:')).toBeLessThan(yaml.indexOf('entities:'))
  })
})
