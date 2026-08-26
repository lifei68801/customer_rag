import { useRef } from 'react'

/**
 * 快速连续操作（翻页、切筛选条件、切租户）会同时有多个请求在途；每次
 * 发起请求前调用 `next()` 递增序号，响应回来时用 `isLatest(id)` 判断
 * 这次响应对应的请求是不是最新那一个——旧请求的响应即使后到，也不会
 * 覆盖新请求已经写入的 state。原本在 GraphReviewsPage.tsx/TermsPage.tsx/
 * DocumentsPage.tsx/DuplicateTermSuggestionsTab.tsx 里各自手写过一份
 * 一模一样的 `useRef(0)` 计数器，这里收成一个共享的最小原语。
 *
 * 返回的对象本身也用 useRef 存着、跨渲染保持同一个引用——调用方常把它
 * 放进 useCallback 的依赖数组，如果每次渲染都返回一个新对象，会导致
 * 依赖它的 useCallback 每次渲染都重新创建，进而级联触发不必要的
 * useEffect 重新执行。
 */
export function useLatestRequestGuard() {
  const counterRef = useRef(0)
  const apiRef = useRef({
    next: () => ++counterRef.current,
    isLatest: (requestId: number) => requestId === counterRef.current,
  })
  return apiRef.current
}
