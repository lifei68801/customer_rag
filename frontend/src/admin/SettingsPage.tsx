import { PAGE_TITLES } from '../adminRoutes'
import { SkinSwitcher } from './SkinSwitcher'
import { DensitySwitcher } from './DensitySwitcher'

/**
 * 账号设置。
 *
 * 这里只放**个人显示偏好**：改错了看着不顺眼，改回来即可，不影响任何
 * 数据。它们此前和「待审关系」并排摆在侧边栏里，占的是工作流的位置。
 *
 * 租户切换不在这里。它是数据作用域——决定你看到的每一条数据、你的写操作
 * 落到哪个租户上。藏进二级页面的代价是往错误的租户里导一批数据，不可
 * 撤销；所以它留在侧边栏顶部常驻。
 */
export function SettingsPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.settings}</h1>
        <p className="text-sm text-ink-soft">
          只影响这台设备上的显示效果，不改变任何数据，也不影响其他人。
        </p>
      </div>

      <div className="flex max-w-md flex-col gap-4 rounded-card border border-subtle bg-card p-4">
        <SkinSwitcher />
        <DensitySwitcher />
      </div>
    </div>
  )
}
