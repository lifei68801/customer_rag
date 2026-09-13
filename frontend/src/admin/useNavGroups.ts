import { useCallback, useEffect, useState } from 'react'
import { groupIdForPath, type NavGroup } from '../adminRoutes'

//: 存的是"用户手动收起了哪些组"。键名沿用旧的，因为它本来就叫 collapsed
//: ——只是此前存的语义是反的（存的是展开过哪些）。旧值被读成"收起这些组"，
//: 影响仅限于这个用户第一次打开新版本时有几组是塌的，点一下就好；为此做
//: 一次迁移不值得。
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
 * **默认全部展开。** 此前的规则是"只展开当前所在的组"，于是落在看板上的
 * 新用户看到的是六个组标题加一个条目——另外十五个功能在折叠的标题后面，
 * 而组标题（「数据审核」「结果预览」）说不出里面具体有什么。功能找不到
 * 的第一原因不是层级太深，是默认藏起来了。
 *
 * 十六项、六组，全展开在一屏里放得下，不需要靠折叠省空间。折叠留给用户
 * 自己按需收，而不是替他决定。
 *
 * 持久化的是"手动收起过哪些组"，但**不记当前所在的那一组**。这不是偷懒：
 * 记了的话，用户临时把当前组折起来看别的，下次再进这一组时它是塌的，当前
 * 页面在导航上无处对应。所以收起当前组只在本次访问里有效。
 *
 * 注意这里不能写成"当前组永远展开"——那样它的标题按钮点了不动，成了一个
 * 看起来能点、实际没反应的控件。
 */
export function useNavGroups(pathname: string) {
  const currentGroup = groupIdForPath(pathname)
  //: 用户手动收起过的组。没设过的组默认展开。
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(readStored().map((id) => [id, true])),
  )

  useEffect(() => {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify(
          Object.entries(collapsed)
            .filter(([id, on]) => on && id !== currentGroup)
            .map(([id]) => id),
        ),
      )
    } catch {
      // 同上：存不进去就算了。
    }
  }, [collapsed, currentGroup])

  const isExpanded = useCallback(
    (group: NavGroup) => !collapsed[group.id],
    [collapsed],
  )

  const toggle = useCallback(
    (group: NavGroup) => setCollapsed((prev) => ({ ...prev, [group.id]: !prev[group.id] })),
    [],
  )

  return { isExpanded, toggle }
}
