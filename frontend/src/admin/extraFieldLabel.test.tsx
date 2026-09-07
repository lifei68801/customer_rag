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
import { fieldDisplayName } from './extraFieldDisplay'

/**
 * 属性字段的「显示名」。
 *
 * 内部名要落进 Cypher、Neo4j 索引和结构化查询的字段名，所以仍然只能是
 * ASCII；界面上和问答里给人看的可以是中文。这个文件钉住：显示名能填、能
 * 提交、能显示，缺失时回退到内部名。
 *
 * 所有用例里 name 和 label 都取不同的值（price / 售价）——取成一样的话，
 * 「用了 label」和「压根没读 label、显示的是 name」两种实现都能通过。
 */

const TERM_TYPES_WITH_LABEL = {
  term_types: [
    {
      value: '商品',
      extra_fields: [{ name: 'price', value_type: 'number', label: '售价' }],
      standard_name_value_type: 'string',
    },
  ],
}

const TERM_TYPES_WITHOUT_LABEL = {
  term_types: [
    {
      value: '商品',
      extra_fields: [{ name: 'price', value_type: 'number', label: '' }],
      standard_name_value_type: 'string',
    },
  ],
}

let termTypesBody: unknown = TERM_TYPES_WITH_LABEL
let lastTermTypeWrite: unknown = null

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

function stubApi() {
  lastTermTypeWrite = null
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.includes('/auth/whoami')) {
        return json({ username: 'admin', role: 'admin', tenant_id: null, current_tenant_id: 'demo' })
      }
      if (url.includes('/nav-badges')) {
        return json({ pending_relations: 0, pending_duplicates: 0, total_terms: 1 })
      }
      if (/\/ontology\/[^/]+\/status$/.test(url)) return json({ confirmed: false })
      if (url.includes('/ontology/') && url.includes('/checkout')) return json({})
      if (url.includes('/terms/summary')) return json({ groups: [{ term_type: '商品', total: 1 }] })
      if (url.includes('/term-types') && (method === 'PUT' || method === 'POST')) {
        lastTermTypeWrite = JSON.parse(String(init?.body ?? '{}'))
        return json({})
      }
      if (url.includes('/term-types')) return json(termTypesBody)
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
  termTypesBody = TERM_TYPES_WITH_LABEL
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

describe('fieldDisplayName', () => {
  it('有显示名时用显示名', () => {
    expect(fieldDisplayName({ name: 'price', label: '售价' })).toBe('售价')
  })

  it('没有显示名时回退到内部名', () => {
    expect(fieldDisplayName({ name: 'price' })).toBe('price')
    expect(fieldDisplayName({ name: 'price', label: '' })).toBe('price')
  })

  it('显示名只有空白时也回退——空白显示名在界面上是一段看不见的空档', () => {
    expect(fieldDisplayName({ name: 'price', label: '   ' })).toBe('price')
  })
})

describe('本体结构页的属性显示名', () => {
  it('编辑已有类型时预填显示名，改完提交时把它一起发出去', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)

    await user.click(await screen.findByRole('button', { name: '编辑' }))
    const labelInput = await screen.findByLabelText('字段显示名')
    expect((labelInput as HTMLInputElement).value).toBe('售价')
    // 内部名跟显示名是两个独立的框：内部名仍是 ASCII，不由显示名生成。
    expect((screen.getByLabelText('字段名') as HTMLInputElement).value).toBe('price')

    await user.clear(labelInput)
    await user.type(labelInput, '当前售价')
    await user.click(screen.getByRole('button', { name: '保存' }))

    expect(lastTermTypeWrite).toMatchObject({
      value: '商品',
      extra_fields: [{ name: 'price', value_type: 'number', label: '当前售价' }],
    })
  })

  it('显示名是空的也能保存——后端不强制，存量字段不会因此卡住', async () => {
    termTypesBody = TERM_TYPES_WITHOUT_LABEL
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.ontology)

    await user.click(await screen.findByRole('button', { name: '编辑' }))
    expect((await screen.findByLabelText('字段显示名')).getAttribute('required')).toBeNull()
    await user.click(screen.getByRole('button', { name: '保存' }))

    expect(lastTermTypeWrite).toMatchObject({
      extra_fields: [{ name: 'price', label: '' }],
    })
  })
})

describe('实体列表的属性显示名', () => {
  it('编辑实体时属性按显示名展示', async () => {
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.terms}?term_type=商品`)

    await user.click(await screen.findByRole('button', { name: '编辑' }))
    const form = await screen.findByRole('textbox', { name: '售价（牛奶）' })
    expect((form as HTMLInputElement).value).toBe('19.99')
    // 内部名不该同时出现在标签上——用户不需要认识它。
    expect(screen.queryByRole('textbox', { name: 'price（牛奶）' })).toBeNull()
  })

  it('没有显示名的字段回退到内部名', async () => {
    termTypesBody = TERM_TYPES_WITHOUT_LABEL
    const user = userEvent.setup()
    renderAt(`${ADMIN_ROUTES.terms}?term_type=商品`)

    await user.click(await screen.findByRole('button', { name: '编辑' }))
    expect(await screen.findByRole('textbox', { name: 'price（牛奶）' })).toBeTruthy()
  })
})
