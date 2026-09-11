import { describe, expect, it } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import { emptyReviewEdit, useReviewEdits } from './useReviewEdits'

describe('useReviewEdits', () => {
  it('没编辑过的记录给一份空的，不是 undefined', () => {
    // 返回 undefined 的话，调用方要在二十几个读取点各写一遍 `?.` 和 `?? ''`
    // ——而漏写一处就是运行时崩溃。
    const { result } = renderHook(() => useReviewEdits())

    expect(result.current.get(999)).toEqual(emptyReviewEdit())
  })

  it('用后端的建议名初始化整批', () => {
    const { result } = renderHook(() => useReviewEdits())

    act(() =>
      result.current.reset([
        { reviewId: 1, subject: 'A', object: 'B' },
        { reviewId: 2, subject: 'C', object: 'D' },
      ]),
    )

    expect(result.current.get(1).subject).toBe('A')
    expect(result.current.get(2).object).toBe('D')
  })

  it('改端点名字，作废那一侧的消歧选择和「新建」标记', () => {
    // 这条是这个 hook 存在的理由。名字变了，之前挑的类型是给旧名字挑的，
    // 之前那个「新建」标记指的是旧名字刚建出来的实体。留着它们，审核员
    // 会拿着为另一个实体做的决定去批准这条边——而界面上一切正常。
    const { result } = renderHook(() => useReviewEdits())

    act(() => result.current.reset([{ reviewId: 1, subject: 'Coffee', object: 'B' }]))
    act(() => result.current.pickType(1, 'subject', '产品'))
    act(() => result.current.markJustCreated(1, 'subject', '类目'))
    expect(result.current.get(1).ambiguityPick.subject).toBe('产品')
    expect(result.current.get(1).justCreated.subject).toBe('类目')

    act(() => result.current.setEndpoint(1, 'subject', 'Latte'))

    expect(result.current.get(1).subject).toBe('Latte')
    expect(result.current.get(1).ambiguityPick.subject).toBeUndefined()
    expect(result.current.get(1).justCreated.subject).toBeUndefined()
  })

  it('只作废被改的那一侧，另一侧留着', () => {
    // 没有这一条的话，"改任何一侧就把两侧都清空"这种实现也能让上面那条
    // 通过——而那会让审核员为另一侧做的决定凭空消失。
    const { result } = renderHook(() => useReviewEdits())

    act(() => result.current.reset([{ reviewId: 1, subject: 'A', object: 'Coffee' }]))
    act(() => result.current.pickType(1, 'object', '类目'))

    act(() => result.current.setEndpoint(1, 'subject', 'Latte'))

    expect(result.current.get(1).ambiguityPick.object).toBe('类目')
  })

  it('一条记录的编辑不影响另一条', () => {
    const { result } = renderHook(() => useReviewEdits())

    act(() =>
      result.current.reset([
        { reviewId: 1, subject: 'A', object: 'B' },
        { reviewId: 2, subject: 'C', object: 'D' },
      ]),
    )
    act(() => result.current.setEndpoint(1, 'subject', 'X'))

    expect(result.current.get(2).subject).toBe('C')
  })

  it('patch 改字段，不触发作废规则', () => {
    // 驳回备注、修复提示这些跟端点身份无关，改它们不该把消歧选择清掉。
    const { result } = renderHook(() => useReviewEdits())

    act(() => result.current.reset([{ reviewId: 1, subject: 'A', object: 'B' }]))
    act(() => result.current.pickType(1, 'subject', '产品'))

    act(() => result.current.patch(1, { rejectNote: '不对' }))

    expect(result.current.get(1).rejectNote).toBe('不对')
    expect(result.current.get(1).ambiguityPick.subject).toBe('产品')
  })

  it('drop 之后回到空态', () => {
    const { result } = renderHook(() => useReviewEdits())

    act(() => result.current.reset([{ reviewId: 1, subject: 'A', object: 'B' }]))
    act(() => result.current.drop(1))

    expect(result.current.get(1)).toEqual(emptyReviewEdit())
  })

  it('reset 丢掉旧的编辑态', () => {
    // 列表刚从后端刷新过，旧的编辑是对上一批记录做的。留着的话，一个
    // 复用了同一个 review_id 的新记录会带着别人的编辑显示出来。
    const { result } = renderHook(() => useReviewEdits())

    act(() => result.current.reset([{ reviewId: 1, subject: 'A', object: 'B' }]))
    act(() => result.current.pickType(1, 'subject', '产品'))

    act(() => result.current.reset([{ reviewId: 1, subject: 'A2', object: 'B2' }]))

    expect(result.current.get(1).subject).toBe('A2')
    expect(result.current.get(1).ambiguityPick.subject).toBeUndefined()
  })
})
