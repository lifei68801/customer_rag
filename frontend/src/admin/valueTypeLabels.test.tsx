import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { resetAdminSession } from './useAdminAuth'
import { valueTypeLabel, valueTypeOptionLabel } from './extraFieldDisplay'

/**
 * 属性取值类型在界面上的说法。
 *
 * 用户问"属性没有 float 类型，无法用于售价和收入"——float 一直都在，它叫
 * number（schema_etl_row_processing.py::convert_field_value 走的就是
 * float()）。看不出 number 是双精度浮点，就等于没有这个类型。存储的枚举值
 * 不动（改了要迁移存量数据），只改界面上的说法。
 */

const TERM_TYPES = {
  term_types: [
    {
      value: '商品',
      extra_fields: [{ name: 'price', value_type: 'number', label: '售价' }],
      standard_name_value_type: 'string',
    },
  ],
}

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function stubApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/auth/whoami')) {
        return json({ username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 1 })
      }
      if (/\/ontology\/[^/]+\/status$/.test(url)) return json({ confirmed: false })
      if (url.includes('/ontology/') && url.includes('/checkout')) return json({})
      if (url.includes('/terms/summary')) return json({ groups: [{ term_type: '商品', total: 1 }] })
      if (url.includes('/term-types')) return json(TERM_TYPES)
      if (url.includes('/relation-types')) return json({ relation_types: [] })
      if (url.includes('/constraints')) return json({ constraints: [] })
      if (url.includes('/terms')) {
        return json({
          terms: [
            {
              node_key: '商品:1',
              standard_name: '牛奶',
              aliases: [],
              term_type: '商品',
              extra_properties: { price: 19.99 },
              source: 'etl',
            },
          ],
          total: 1,
        })
      }
      return new Promise(() => {})
    }),
  )
}

beforeEach(() => {
  resetAdminSession()
  sessionStorage.clear()
  localStorage.clear()
  stubApi()
})

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

describe('取值类型的界面文案', () => {
  it('下拉选项说清 number 是小数，并给一个金额例子', () => {
    expect(valueTypeOptionLabel('number')).toBe('小数（如 19.99，售价/金额）')
    expect(valueTypeOptionLabel('integer')).toBe('整数')
    expect(valueTypeOptionLabel('string')).toBe('文本')
    expect(valueTypeOptionLabel('number[]')).toBe('小数列表')
  })

  it('行内提示用短说法，词汇跟下拉一致', () => {
    expect(valueTypeLabel('number')).toBe('小数')
    expect(valueTypeLabel('integer')).toBe('整数')
    expect(valueTypeLabel('string')).toBe('文本')
    expect(valueTypeLabel('number[]')).toBe('小数列表')
  })

  it('认不出的枚举值原样显示——后端加了新类型时不该显示成一片空白', () => {
    expect(valueTypeLabel('geo_point')).toBe('geo_point')
    expect(valueTypeOptionLabel('geo_point')).toBe('geo_point')
  })

  it('date 有中文说法，不是裸的 date', () => {
    // 认不出的枚举值会原样显示，用户在下拉里看到的就是 "date"。
    expect(valueTypeLabel('date')).toBe('日期')
    expect(valueTypeOptionLabel('date')).toMatch(/2026-01-05/)
  })
})

describe('本体结构页的类型下拉', () => {
  it('选项文案说人话，提交出去的值仍是存储枚举', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)
    await user.click(await screen.findByRole('button', { name: '编辑' }))

    const fieldType = (await screen.findByLabelText('字段类型')) as HTMLSelectElement
    const numberOption = Array.from(fieldType.options).find((o) => o.value === 'number')
    expect(numberOption?.textContent).toBe('小数（如 19.99，售价/金额）')
    // 存储枚举不动：改了要迁移存量数据。date 是这次新加的可选类型。
    expect(Array.from(fieldType.options).map((o) => o.value)).toEqual([
      'string',
      'number',
      'integer',
      'number[]',
      'date',
    ])
  })

  it('「自身取值类型」下拉用同一套说法', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)
    await user.click(await screen.findByRole('button', { name: '编辑' }))

    const select = (await screen.findByLabelText('自身取值类型')) as HTMLSelectElement
    expect(
      Array.from(select.options).find((o) => o.value === 'number')?.textContent,
    ).toBe('小数（如 19.99，售价/金额）')
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      'string',
      'number',
      'integer',
    ])
  })
})

describe('实体明细的属性类型提示', () => {
  it('属性旁边的类型提示也说人话', async () => {
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.terms}?term_type=商品`)
    await user.click(await screen.findByRole('button', { name: '编辑' }))

    const label = (await screen.findByRole('textbox', { name: '售价（牛奶）' })).closest('label')
    expect(label?.textContent).toContain('小数')
    expect(label?.textContent).not.toContain('number')
  })
})
