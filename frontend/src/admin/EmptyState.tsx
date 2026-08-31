import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'

interface Props {
  /** 图标。用 lucide 的组件本身（如 `Inbox`），不是渲染好的元素。 */
  icon: LucideIcon
  /** 一句话说清"这里为什么空"。 */
  title: string
  /**
   * 下一步做什么。**这是这个组件存在的理由**——散装的"暂无数据"只告诉用户
   * 屏幕是空的，不告诉他该干什么。强制这个字段是为了逼着调用方回答那个问题；
   * 确实没有下一步时（比如历史记录天然为空）传 null，那是个显式的判断，
   * 不是忘了写。
   */
  action: ReactNode | null
}

/**
 * 空状态的统一形态：图标 + 标题 + 下一步。
 *
 * 结构借鉴 Palantir Blueprint 的 NonIdealState（Apache-2.0，
 * https://blueprintjs.com/docs/#core/components/non-ideal-state）——只取它
 * 的信息结构，样式沿用本项目的设计令牌，不引入那个库（它自带的一整套
 * 样式会跟 Tailwind 打架）。
 *
 * 收编之前散在 8 处、各写各的"暂无数据"。统一样式只是副产品，真正的收益
 * 是 action 这个必填字段带来的：它把"这里是空的"改写成"你可以做什么"。
 */
export function EmptyState({ icon: Icon, title, action }: Props) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-card border border-subtle bg-card px-6 py-10 text-center">
      <Icon aria-hidden="true" className="h-8 w-8 text-ink-soft" strokeWidth={1.5} />
      <p className="text-sm font-bold text-ink">{title}</p>
      {action && <div className="text-sm text-ink-soft">{action}</div>}
    </div>
  )
}
