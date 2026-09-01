import { Compass } from 'lucide-react'
import { Link } from 'react-router-dom'
import { NAV_GROUPS } from '../adminRoutes'
import { EmptyState } from './EmptyState'

/**
 * 后台的兜底页。
 *
 * 此前 App.tsx 没有 `path="*"`，敲错的 /admin/* 会渲染成一片空白——既不
 * 报错也不跳转，看起来像页面挂了。路由这次重命名后旧书签更容易敲歪，
 * 白屏的代价比以前大。
 *
 * 不做自动跳转：用户可能是从别人分享的链接进来的，直接弹回首页会让他
 * 以为链接本身是对的、只是自己点错了。停在这里、把四个阶段摊开，让他
 * 自己认出要去哪。
 */
export function NotFoundPage() {
  // 包一层带 testid 的容器：侧边栏也会渲染同样的分组名，测试需要把断言
  // 限定在这块里，否则查"接入数据"会同时命中导航和这里。
  return (
    <div data-testid="not-found">
    <EmptyState
      icon={Compass}
      title="这个页面不存在"
      action={
        <div className="flex flex-col gap-3">
          <p>地址可能过期了，或者拼写有误。从这里挑一个：</p>
          <div className="flex flex-col gap-2 text-left">
            {NAV_GROUPS.map((group) => (
              <div key={group.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="text-xs font-bold uppercase tracking-wide text-ink-soft">
                  {group.label}
                </span>
                {group.items.map((item) => (
                  <Link
                    key={item.path}
                    to={item.path}
                    className="font-bold text-ink underline underline-offset-2 hover:text-accent-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
                  >
                    {item.label}
                  </Link>
                ))}
              </div>
            ))}
          </div>
        </div>
      }
    />
    </div>
  )
}
