import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ChatInput } from './ChatInput'

/**
 * 会话输入框。
 *
 * 从单行 `<input>` 换成可增高的 `<textarea>`：长问题在单行框里只能横向
 * 滚动，用户看不到自己写了什么，也没法换行分段。
 *
 * 回车即发送，所以**必须挡住输入法的组词态**——中文选字过程中的回车是
 * "确认候选"，不是"发送"。不挡的话用户打到一半按回车选词，半句话就飞出去了。
 */

describe('ChatInput', () => {
  it('Enter 发送', async () => {
    const onSend = vi.fn()
    const user = userEvent.setup()
    render(<ChatInput disabled={false} onSend={onSend} />)

    await user.type(screen.getByPlaceholderText(/输入你的问题/), '网关超时是什么意思')
    await user.keyboard('{Enter}')

    expect(onSend).toHaveBeenCalledWith('网关超时是什么意思')
  })

  it('Shift+Enter 换行，不发送', async () => {
    const onSend = vi.fn()
    const user = userEvent.setup()
    render(<ChatInput disabled={false} onSend={onSend} />)

    const box = screen.getByPlaceholderText(/输入你的问题/)
    await user.type(box, '第一行')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    await user.type(box, '第二行')

    expect(onSend).not.toHaveBeenCalled()
    expect((box as HTMLTextAreaElement).value).toBe('第一行\n第二行')
  })

  it('输入法组词时的回车不发送', () => {
    // 这条是中文用户每天都会踩的那个：拼音选字按回车，本意是确认候选词。
    // 当成发送的话，飞出去的是一句没写完的话，而且输入框还被清空了。
    const onSend = vi.fn()
    render(<ChatInput disabled={false} onSend={onSend} />)

    const box = screen.getByPlaceholderText(/输入你的问题/)
    fireEvent.change(box, { target: { value: '网关' } })
    fireEvent.compositionStart(box)
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(onSend).not.toHaveBeenCalled()
  })

  it('组词结束之后的回车正常发送', () => {
    // 没有这一条的话，"组过词就永远不再发送"这种实现也能让上面那条通过。
    const onSend = vi.fn()
    render(<ChatInput disabled={false} onSend={onSend} />)

    const box = screen.getByPlaceholderText(/输入你的问题/)
    fireEvent.change(box, { target: { value: '网关超时' } })
    fireEvent.compositionStart(box)
    fireEvent.compositionEnd(box)
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(onSend).toHaveBeenCalledWith('网关超时')
  })

  it('只有空白时发不出去', async () => {
    const onSend = vi.fn()
    const user = userEvent.setup()
    render(<ChatInput disabled={false} onSend={onSend} />)

    await user.type(screen.getByPlaceholderText(/输入你的问题/), '   ')
    await user.keyboard('{Enter}')

    expect(onSend).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
  })

  it('正在回答时发不出第二条', async () => {
    const onSend = vi.fn()
    const user = userEvent.setup()
    render(<ChatInput disabled onSend={onSend} />)

    const box = screen.getByPlaceholderText(/输入你的问题/)
    expect(box).toBeDisabled()
    await user.keyboard('{Enter}')

    expect(onSend).not.toHaveBeenCalled()
  })

  it('发送之后清空输入框', async () => {
    const user = userEvent.setup()
    render(<ChatInput disabled={false} onSend={vi.fn()} />)

    const box = screen.getByPlaceholderText(/输入你的问题/)
    await user.type(box, '问题')
    await user.keyboard('{Enter}')

    expect((box as HTMLTextAreaElement).value).toBe('')
  })

  it('把快捷键说出来，不让用户靠试', () => {
    render(<ChatInput disabled={false} onSend={vi.fn()} />)

    expect(screen.getByText(/Enter 发送/)).toBeTruthy()
    expect(screen.getByText(/Shift \+ Enter 换行/)).toBeTruthy()
  })
})
