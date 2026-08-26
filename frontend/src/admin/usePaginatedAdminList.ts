import { useCallback, useEffect, useState } from 'react'
import { useLatestRequestGuard } from './useLatestRequestGuard'

/**
 * "拉一个分页列表，翻页自动重取，竞态保护"这个模式在
 * DuplicateTermSuggestionsTab.tsx/TermsPage.tsx 里各自手写过一份。这个
 * 钩子把它收成一个共享的深模块：`fetchPage` 由调用方注入，已经把各自
 * 端点的响应体（字段名各不相同，比如 `{terms, total}` vs
 * `{suggestions, total}`）归一化成 `{items, total}`——钩子本身不关心
 * 响应体长什么样，只管请求序号保护+分页状态+自动重取。
 *
 * `error`/`setError` 暴露读写两端，不是只读：调用方常常需要在同一块
 * 错误横幅里既展示"列表加载失败"，也展示"批准/驳回/编辑/删除失败"这类
 * 相关操作的错误（复用同一个视觉位置，而不是另开一块），所以 setter
 * 也要能从外面调用。
 */
export function usePaginatedAdminList<T>(
  fetchPage: (page: number) => Promise<{ items: T[]; total: number }>,
) {
  const [items, setItems] = useState<T[]>([])
  const [total, setTotal] = useState(0)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const guard = useLatestRequestGuard()

  const refresh = useCallback(async () => {
    const requestId = guard.next()
    setError(null)
    try {
      const result = await fetchPage(page)
      if (!guard.isLatest(requestId)) return
      setItems(result.items)
      setTotal(result.total)
    } catch (err) {
      if (!guard.isLatest(requestId)) return
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      if (guard.isLatest(requestId)) {
        setLoaded(true)
      }
    }
  }, [fetchPage, page])

  useEffect(() => {
    refresh()
  }, [refresh])

  return { items, total, loaded, error, setError, page, setPage, refresh }
}
