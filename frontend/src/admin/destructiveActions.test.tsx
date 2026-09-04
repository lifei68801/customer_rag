import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES } from '../adminRoutes'
import { CONFIRM_IRREVERSIBLE_HINT } from './OntologySchemaPage'
import { resetAdminSession } from './useAdminAuth'

/**
 * 身份不再存 sessionStorage（token 在 HttpOnly Cookie 里，JS 读不到，也
 * 塞不进去）：界面从 whoami 拿身份，所以这里要打桩的是 whoami。
 */
function whoamiResponse() {
  return Promise.resolve(
    new Response(
      JSON.stringify({
        username: 'alice',
        role: 'member',
        tenant_id: 'demo',
        current_tenant_id: 'demo',
      }),
      { status: 200 },
    ),
  )
}

/**
 * 不可逆的动作不该长得像安全的动作。
 *
 * 「确认 schema」的效果是「旧的已确认版本会被换掉、无法恢复」，按钮却是
 * 成功色的绿色。确认弹窗把后果写得很清楚，但按钮本身在暗示这是一个安全
 * 操作——用户在点开弹窗之前就已经形成了预期。
 */

beforeEach(() => {
  resetAdminSession()
  localStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) =>
      String(input).includes('/auth/whoami') ? whoamiResponse() : new Promise(() => {}),
    ),
  )
})

// 会话状态是异步的（身份从 whoami 读，token 在 HttpOnly Cookie 里 JS 读不
// 到），后台外壳要等 whoami 回来才画得出来。不等的话断言会对着一棵空树跑。
async function renderAt(path: string) {
  const result = render(
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
  await screen.findByTestId('admin-topbar')
  return result
}

describe('确认 schema', () => {
  it('用危险色，不用成功色', async () => {
    await renderAt(ADMIN_ROUTES.ontology)
    const button = screen.getByRole('button', { name: /确认 schema/ })
    expect(button.className, '不可逆的动作用了成功色').not.toMatch(/status-success/)
    expect(button.className, '没有用危险色').toMatch(/status-error|danger/)
  })

  it('总有 title 说明情况，不只靠颜色', async () => {
    // 颜色不能是唯一的信号：色觉障碍的用户看到的是两个灰按钮。
    //
    // title 分两种情况：点不了的时候说为什么点不了，能点的时候说点下去
    // 会发生什么。这里数据还在加载、前置条件不满足，所以是前者——两种
    // 都不能是空的。
    await renderAt(ADMIN_ROUTES.ontology)
    const button = screen.getByRole('button', { name: /确认 schema/ })
    expect(button.getAttribute('title')).toBeTruthy()
  })

  it('可用时的 title 说明这一步不可逆', async () => {
    // 从源码 import 而不是在测试里抄一份：抄的那份改了源码也不会红，
    // 等于没测。这里绕开「把页面推到前置条件全满足」是有意的——那要
    // stub 掉本体的四个接口，而这条关心的只是文案本身。
    expect(CONFIRM_IRREVERSIBLE_HINT).toMatch(/不可逆|无法恢复|不能撤销/)
  })
})

describe('本体图只有一个入口', () => {
  it('约束区不再自带一份图，换成去图页面的链接', async () => {
    // 扫源码而不是查 DOM：约束是第三个 tab，默认不挂载，查 DOM 的话
    // 「找不到那个切换控件」在修好之前也成立——又是一次假绿。
    const source = readFileSync(join(__dirname, 'OntologySchemaPage.tsx'), 'utf8')
    expect(source, '约束区还留着「表格/图」切换').not.toMatch(/约束视图形态/)
    expect(source, '没有给出去本体图的链接').toMatch(/ADMIN_ROUTES\.ontologyGraph/)
  })

  it('图组件不再被本体页直接渲染', async () => {
    // 独立页面之外再留一份 tab 内的图，就是同一个东西的两个入口：两处
    // 状态、两处要改，而且 tab 里那份没有自己的 URL，分享不出去。
    const source = readFileSync(join(__dirname, 'OntologySchemaPage.tsx'), 'utf8')
    expect(source, '还在渲染 <OntologyGraph>').not.toMatch(/<OntologyGraph/)
  })
})
