import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, NAV_GROUPS } from '../adminRoutes'
import { commandPaletteHint } from './shortcutHint'

/**
 * ⌘K 里的导航条目。
 *
 * 这份表此前是手写的，跟侧边栏各写各的：它有「数据加工」「知识图谱审核」
 * 这些已经不存在的名字，缺「本体图」「疑似重复」「表格导入」，还有一条
 * 指向一个从来没存在过的路径。手写的表不会在改路由时报错。
 */

beforeEach(() => {
  sessionStorage.setItem('admin_session_token', 'test-token')
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
})

function Probe() {
  return <span data-testid="url">{useLocation().pathname}</span>
}

function renderAt(path: string) {
  return render(
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={[path]}>
            <Probe />
            <App />
          </MemoryRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>,
  )
}

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard('{Meta>}k{/Meta}')
  // 面板是懒加载的，第一次按下要等它到位。超时给到 5s：全量并发跑时
  // 这个 chunk 的加载会超过 waitFor 默认的 1s，单独跑却总是通过——
  // 典型的只在 CI 上红的那种 flaky。
  await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy(), { timeout: 5000 })
}

describe('导航命令', () => {
  it('七个目的地一个不少，名字跟侧边栏一致', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await openPalette(user)
    for (const item of NAV_GROUPS.flatMap((g) => g.items)) {
      expect(screen.getByRole('option', { name: new RegExp(item.label) })).toBeTruthy()
    }
  })

  it('没有已经不存在的旧名字', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await openPalette(user)
    for (const stale of ['数据加工', '知识图谱审核', '本体管理', '文档管理']) {
      expect(screen.queryByRole('option', { name: stale }), `残留旧名字「${stale}」`).toBeNull()
    }
  })

  it('选中真的跳过去', async () => {
    const user = userEvent.setup()
    renderAt(ADMIN_ROUTES.documents)
    await openPalette(user)
    await user.click(screen.getByRole('option', { name: /疑似重复/ }))
    expect(screen.getByTestId('url').textContent).toBe(ADMIN_ROUTES.reviewDuplicates)
  })
})

describe('快捷键提示按平台显示', () => {
  it('Windows/Linux 上写 Ctrl+K——写死 ⌘K 的话用户照着按不出来', () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('Win32')
    expect(commandPaletteHint()).toBe('Ctrl+K')
  })

  it('Mac 上写 ⌘K', () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel')
    expect(commandPaletteHint()).toBe('⌘K')
  })

  it('平台判不出来时说 Ctrl——Mac 按 Ctrl+K 也能开，反过来无键可按', () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('')
    expect(commandPaletteHint()).toBe('Ctrl+K')
  })

  it('侧边栏的提示用的就是这个值，不是另抄一份字面量', async () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('Win32')
    renderAt(ADMIN_ROUTES.documents)
    const hint = await screen.findByTestId('command-palette-hint')
    expect(hint.textContent).toBe('Ctrl+K')
  })
})
