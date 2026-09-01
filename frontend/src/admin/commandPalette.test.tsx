import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from '../App'
import { SkinProvider } from './SkinContext'
import { ConfirmProvider } from './ConfirmContext'
import { ToastProvider } from './ToastContext'
import { ADMIN_ROUTES, NAV_GROUPS } from '../adminRoutes'

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
  // 面板是懒加载的，第一次按下要等它到位。
  await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy())
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
