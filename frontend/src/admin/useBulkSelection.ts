import { useCallback, useMemo, useState } from 'react'
import type { BulkDeleteFilters, BulkDeleteTarget } from './bulkDelete'

/**
 * 两级全选的选中状态（Gmail / GitHub 那套）：
 *
 * * `keys`——勾了本页的若干条，前端手里就是这几个 key；
 * * `all`——改成了「当前筛选条件下的全部」，前端手里只有筛选条件和总条数，
 *   具体是哪些由服务端展开。
 *
 * 两者是不同的状态而不是同一个集合的大小差别：它们的破坏力差两个数量级，
 * 界面上必须一眼可辨，请求体也是两种互斥的模式。
 *
 * scopeId 把选中状态钉在某一段列表上（平铺列表是 'flat'，分组视图里是各自的
 * 实体类型）。同一时刻只有一段能有选中项：跨段的多选在这里没有真实用途，
 * 却会让「全部」到底指哪个筛选条件变得含糊——而那正是不能含糊的那件事。
 */
export type BulkSelection =
  | { kind: 'none' }
  | { kind: 'keys'; scopeId: string; keys: string[] }
  | { kind: 'all'; scopeId: string; filters: BulkDeleteFilters; total: number }

export interface BulkSelectionApi {
  selection: BulkSelection
  /** 这一段里选中了几条（'all' 模式下是筛选条件下的总条数）。 */
  countIn: (scopeId: string) => number
  isSelected: (scopeId: string, key: string) => boolean
  /** 本页某一条的勾选/取消。 */
  toggleKey: (scopeId: string, key: string) => void
  /** 表头复选框：这一段列出的这些条，全选或全不选。 */
  togglePage: (scopeId: string, keys: string[]) => void
  /** 升级成「当前筛选条件下的全部」。 */
  selectAllMatching: (scopeId: string, filters: BulkDeleteFilters, total: number) => void
  clear: () => void
  /** 当前选中折算成一次批量删除请求的目标；没选中时是 null。 */
  targetFor: (scopeId: string) => BulkDeleteTarget | null
}

export function useBulkSelection(): BulkSelectionApi {
  const [selection, setSelection] = useState<BulkSelection>({ kind: 'none' })

  const countIn = useCallback(
    (scopeId: string) => {
      if (selection.kind === 'none' || selection.scopeId !== scopeId) return 0
      return selection.kind === 'keys' ? selection.keys.length : selection.total
    },
    [selection],
  )

  const isSelected = useCallback(
    (scopeId: string, key: string) => {
      if (selection.kind === 'none' || selection.scopeId !== scopeId) return false
      // 'all' 模式下每一行都是选中的：界面上一行没勾、却要被这次删除带走，
      // 是用户最容易据此得出错误结论的地方。
      if (selection.kind === 'all') return true
      return selection.keys.includes(key)
    },
    [selection],
  )

  const toggleKey = useCallback((scopeId: string, key: string) => {
    setSelection((prev) => {
      // 从「全部」退回逐条时只留这一条：保留原来的 4712 条再减一条，
      // 既说不清也做不到（那些 key 前端根本没有）。
      const current =
        prev.kind === 'keys' && prev.scopeId === scopeId ? prev.keys : []
      const next = current.includes(key)
        ? current.filter((k) => k !== key)
        : [...current, key]
      return next.length === 0 ? { kind: 'none' } : { kind: 'keys', scopeId, keys: next }
    })
  }, [])

  const togglePage = useCallback((scopeId: string, keys: string[]) => {
    setSelection((prev) => {
      const allSelected =
        prev.kind === 'keys' &&
        prev.scopeId === scopeId &&
        keys.length > 0 &&
        keys.every((k) => prev.keys.includes(k))
      if (allSelected) return { kind: 'none' }
      return keys.length === 0 ? { kind: 'none' } : { kind: 'keys', scopeId, keys: [...keys] }
    })
  }, [])

  const selectAllMatching = useCallback(
    (scopeId: string, filters: BulkDeleteFilters, total: number) => {
      setSelection({ kind: 'all', scopeId, filters, total })
    },
    [],
  )

  const clear = useCallback(() => setSelection({ kind: 'none' }), [])

  const targetFor = useCallback(
    (scopeId: string): BulkDeleteTarget | null => {
      if (selection.kind === 'none' || selection.scopeId !== scopeId) return null
      if (selection.kind === 'keys') return { mode: 'keys', keys: selection.keys }
      return { mode: 'filters', filters: selection.filters, total: selection.total }
    },
    [selection],
  )

  return useMemo(
    () => ({
      selection,
      countIn,
      isSelected,
      toggleKey,
      togglePage,
      selectAllMatching,
      clear,
      targetFor,
    }),
    [selection, countIn, isSelected, toggleKey, togglePage, selectAllMatching, clear, targetFor],
  )
}
