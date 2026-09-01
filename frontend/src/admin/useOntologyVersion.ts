import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { ViewMode } from './ontologyTypes'

/**
 * 草稿还是已确认——存在 URL 里，不在组件里。
 *
 * 此前本体结构页和本体图各有一份 useState：在一边切到已确认，跳到另一
 * 边又回到草稿，同一个问题两个答案。而且这个状态没有地址，截图发给同事，
 * 对方打开看到的是另一份数据。
 *
 * 未知值一律当草稿：草稿是可编辑的那份，看错了改得回来；把 `?version=x`
 * 当成已确认才危险——用户以为在编辑草稿，实际在只读快照上白忙。
 */
export function useOntologyVersion(): [ViewMode, (next: ViewMode) => void] {
  const [params, setParams] = useSearchParams()
  const version: ViewMode = params.get('version') === 'confirmed' ? 'confirmed' : 'draft'

  const setVersion = useCallback(
    (next: ViewMode) => {
      const updated = new URLSearchParams(params)
      // 草稿是默认值，不写进 URL——不然每个地址都拖着一段没有信息量的
      // 参数，还会让"没带 version 的链接"看起来像是漏了什么。
      if (next === 'confirmed') updated.set('version', 'confirmed')
      else updated.delete('version')
      setParams(updated, { replace: true })
    },
    [params, setParams],
  )

  return [version, setVersion]
}
