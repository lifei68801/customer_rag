import { describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { useEditableList } from './useEditableList'

function deferred() {
  let resolve!: () => void
  let reject!: (err: unknown) => void
  const promise = new Promise<void>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('useEditableList', () => {
  it('一开始什么表单都没开、也没有请求在飞', () => {
    const { result } = renderHook(() => useEditableList())

    expect(result.current.form).toEqual({ kind: 'idle' })
    expect(result.current.busy).toBe(false)
  })

  it('四种表单互斥——开一个就关掉上一个', () => {
    // 拆成 creating / editingValue 两个变量时，"同时在新建又在编辑"是一个
    // 可表达的状态，而界面上那时会同时画出两个表单。
    const { result } = renderHook(() => useEditableList())

    act(() => result.current.openCreate())
    expect(result.current.form).toEqual({ kind: 'creating' })

    act(() => result.current.openEdit('产品'))
    expect(result.current.form).toEqual({ kind: 'editing', key: '产品' })

    act(() => result.current.openMigrate('产品'))
    expect(result.current.form).toEqual({ kind: 'migrating', from: '产品' })

    act(() => result.current.close())
    expect(result.current.form).toEqual({ kind: 'idle' })
  })

  it('请求在飞的时候 busy 为真，跑完复位', async () => {
    const { result } = renderHook(() => useEditableList())
    const gate = deferred()

    let finished: Promise<boolean>
    act(() => {
      finished = result.current.run('delete', '产品', () => gate.promise)
    })
    expect(result.current.busy).toBe(true)
    expect(result.current.isDeleting('产品')).toBe(true)
    expect(result.current.isDeleting('客户')).toBe(false)

    await act(async () => {
      gate.resolve()
      await finished
    })
    expect(result.current.busy).toBe(false)
    expect(result.current.isDeleting('产品')).toBe(false)
  })

  it('已经有请求在飞时，第二个操作不跑', async () => {
    // 这是这个 hook 存在的理由。此前这条不变量散在十几个 disabled 表达式和
    // 六个函数开头的早退里，而且并不一致：删除在飞的时候迁移按钮照样能点。
    const { result } = renderHook(() => useEditableList())
    const gate = deferred()
    const second = vi.fn(async () => {})

    let first: Promise<boolean>
    act(() => {
      first = result.current.run('delete', '产品', () => gate.promise)
    })

    let accepted: boolean | undefined
    await act(async () => {
      accepted = await result.current.run('migrate', '产品', second)
    })

    expect(accepted).toBe(false)
    expect(second).not.toHaveBeenCalled()

    await act(async () => {
      gate.resolve()
      await first
    })
  })

  it('操作失败之后要复位，不能把整个 tab 永久锁住', async () => {
    // finally 里复位而不是成功路径上复位：一次网络失败把 busy 永久留在 true
    // 上，用户就只能刷新页面——而他看到的是所有按钮都灰着，没有任何解释。
    const { result } = renderHook(() => useEditableList())

    await act(async () => {
      await expect(
        result.current.run('save', '产品', async () => {
          throw new Error('boom')
        }),
      ).rejects.toThrow('boom')
    })

    expect(result.current.busy).toBe(false)
  })

  it('异常原样抛出去，不在这里吞掉', async () => {
    // 各 tab 的错误处理不同（实体类型那边要认 409 里带的挡路术语），
    // 吞掉异常会让那些分支没法写。
    const { result } = renderHook(() => useEditableList())

    await act(async () => {
      await expect(
        result.current.run('delete', 'x', async () => {
          throw new Error('409')
        }),
      ).rejects.toThrow('409')
    })
  })

  it('isEditing / isSaving 只对那一行为真', async () => {
    const { result } = renderHook(() => useEditableList())

    act(() => result.current.openEdit('产品'))
    expect(result.current.isEditing('产品')).toBe(true)
    expect(result.current.isEditing('客户')).toBe(false)

    const gate = deferred()
    let running: Promise<boolean>
    act(() => {
      running = result.current.run('save', '产品', () => gate.promise)
    })
    expect(result.current.isSaving('产品')).toBe(true)
    expect(result.current.isSaving('客户')).toBe(false)
    // 存的时候不算"在删"——按钮文案靠这个区分「保存中…」和「删除中…」。
    expect(result.current.isDeleting('产品')).toBe(false)

    await act(async () => {
      gate.resolve()
      await running
    })
  })
})
