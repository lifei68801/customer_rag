import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { EntityMappingEditor } from './EntityMappingEditor'
import type { BuilderEntity, ConfirmedTermType } from './types'

/**
 * ETL 映射编辑器里的属性字段既要看得懂，又要能对上 YAML。
 *
 * 显示名给人认（「售价」），内部名是真正写进 ETL 配置的键（price）——两个
 * 都得在场：只显示内部名用户认不出这是哪个属性，只显示显示名用户对不上
 * 下载下来的 YAML。
 */

const TERM_TYPES: ConfirmedTermType[] = [
  {
    value: '商品',
    extra_fields: [
      { name: 'price', value_type: 'number', label: '售价' },
      { name: 'sku_code', value_type: 'string' },
    ],
  },
]

const ENTITY: BuilderEntity = {
  id: 'e1',
  termType: '商品',
  fileId: null,
  standardNameColumn: '',
  nodeKeyParts: [],
  fieldMappings: {},
}

function renderEditor() {
  return render(
    <EntityMappingEditor
      entity={ENTITY}
      files={[]}
      termTypes={TERM_TYPES}
      onChange={vi.fn()}
      onRemove={vi.fn()}
    />,
  )
}

describe('属性字段映射的字段名展示', () => {
  it('有显示名时两个名字都在场', () => {
    renderEditor()
    const row = screen.getByTestId('field-mapping-price')
    expect(row.textContent).toContain('售价')
    expect(row.textContent).toContain('price')
  })

  it('没有显示名时内部名只出现一次，不重复成 "sku_code sku_code"', () => {
    renderEditor()
    const row = screen.getByTestId('field-mapping-sku_code')
    expect(row.textContent?.match(/sku_code/g)?.length).toBe(1)
    expect(row.textContent).not.toContain('（）')
  })
})
