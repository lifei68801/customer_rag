import { describe, expect, it } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ConfirmProvider, useConfirm } from './ConfirmContext'

/**
 * 确认弹窗的焦点管理。
 *
 * 这是产品里风险最高的控件——「此操作不可撤销」那一类操作全走它。而 WAI-ARIA
 * 的 alertdialog 要求的三件事此前一件都没做：焦点移进来、Tab 不走出去、关闭
 * 时还回去。
 *
 * 后果都不是理论上的：焦点停在遮罩后面那个「删除」按钮上时，读屏软件不播报
 * 弹窗内容，用户听不到自己正要确认什么；Tab 走出去之后，他在被遮罩盖住的、
 * 看不见的控件之间移动。
 */

function Harness() {
  const confirm = useConfirm()
  return (
    <div>
      <button type="button" onClick={() => void confirm('确定要删除吗？')}>
        删除
      </button>
      <button type="button">别的按钮</button>
    </div>
  )
}

function renderHarness() {
  return render(
    <ConfirmProvider>
      <Harness />
    </ConfirmProvider>,
  )
}

describe('确认弹窗的焦点', () => {
  it('打开时落在「取消」上，不是「确认」', async () => {
    // 确认那颗是危险的那颗。焦点默认停在它上面，等于邀请用户顺手回车。
    const user = userEvent.setup()
    renderHarness()

    await user.click(screen.getByRole('button', { name: '删除' }))

    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole('button', { name: '取消' }))
    })
  })

  it('Tab 在弹窗里循环，不走到遮罩后面去', async () => {
    // 走出去的话，用户在看不见的控件之间 Tab，而屏幕上的弹窗还开着。
    const user = userEvent.setup()
    renderHarness()
    await user.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() => expect(screen.getByRole('alertdialog')).toBeTruthy())

    const cancel = screen.getByRole('button', { name: '取消' })
    const ok = screen.getByRole('button', { name: '确认' })

    await user.tab()
    expect(document.activeElement).toBe(ok)
    // 再按一次应该绕回「取消」，而不是跑到「别的按钮」上。
    await user.tab()
    expect(document.activeElement).toBe(cancel)
  })

  it('Shift+Tab 从第一个绕到最后一个', async () => {
    const user = userEvent.setup()
    renderHarness()
    await user.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() => expect(screen.getByRole('alertdialog')).toBeTruthy())

    await user.tab({ shift: true })

    expect(document.activeElement).toBe(screen.getByRole('button', { name: '确认' }))
  })

  it('关闭之后焦点还给打开它的那个按钮', async () => {
    // 不还的话焦点落到 body，下一次 Tab 从页面顶部重新开始——用户刚才在
    // 哪儿全丢了。
    const user = userEvent.setup()
    renderHarness()
    const trigger = screen.getByRole('button', { name: '删除' })
    await user.click(trigger)
    await waitFor(() => expect(screen.getByRole('alertdialog')).toBeTruthy())

    await user.click(screen.getByRole('button', { name: '取消' }))

    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('按 Esc 关闭时焦点也还回去', async () => {
    const user = userEvent.setup()
    renderHarness()
    const trigger = screen.getByRole('button', { name: '删除' })
    await user.click(trigger)
    await waitFor(() => expect(screen.getByRole('alertdialog')).toBeTruthy())

    await user.keyboard('{Escape}')

    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })

  it('触发按钮已经不在了也不报错', async () => {
    // 常见：点「删除」→ 确认 → 那一行连同按钮一起被移除。往一个已经脱离
    // 文档的元素上 focus() 是空操作，但读它的属性前必须先确认它还连着。
    const user = userEvent.setup()
    const { unmount } = renderHarness()
    await user.click(screen.getByRole('button', { name: '删除' }))
    await waitFor(() => expect(screen.getByRole('alertdialog')).toBeTruthy())

    expect(() => unmount()).not.toThrow()
  })
})
