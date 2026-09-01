import { useCallback, useEffect, useState } from 'react'
import { groupIdForPath, type NavGroup } from '../adminRoutes'

const STORAGE_KEY = 'admin_nav_collapsed'

function readStored(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    // 存储被禁用、配额满、或者存进去的是别的版本写的垃圾。展开状态是个
    // 便利，不值得为它炸掉整个后台。
    return []
  }
}

/**
 * 侧边栏分组的展开状态。
 *
 * 两条规则：当前所在的组默认展开（用户不必记得自己在哪段流程里），其余
 * 默认收起。
 *
 * 持久化的只有"手动展开过哪些组"，**不记录手动收起**。这不是偷懒——
 * 记了收起的话，用户临时把当前组折起来看别的，下次再进这一组时它是塌
 * 的，当前页面在导航上无处对应。收起当前组因此只在本次访问里有效。
 */
export function useNavGroups(pathname: string) {
  const currentGroup = groupIdForPath(pathname)
  // 只有显式设过的组才在这里有值；其余落到"是不是当前组"这条默认规则上。
  const [manual, setManual] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(readStored().map((id) => [id, true])),
  )

  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify(
          Object.entries(manual)
            .filter(([, on]) => on)
            .map(([id]) => id),
        ),
      )
    } catch {
      // 同上：存不进去就算了。
    }
  }, [manual])

  const isExpanded = useCallback(
    (group: NavGroup) => manual[group.id] ?? group.id === currentGroup,
    [manual, currentGroup],
  )

  const toggle = useCallback(
    (group: NavGroup) =>
      setManual((prev) => ({ ...prev, [group.id]: !(prev[group.id] ?? group.id === currentGroup) })),
    [currentGroup],
  )

  return { isExpanded, toggle }
}
