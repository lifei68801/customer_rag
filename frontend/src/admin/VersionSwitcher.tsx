import { useEffect, useRef } from 'react'
import { useAdminTenant } from './TenantContext'
import { useOntologyVersion } from './useOntologyVersion'

const segmentClass = (active: boolean) =>
  `min-h-[32px] flex-1 cursor-pointer px-2 text-xs font-bold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
    active ? 'bg-ink text-paper' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

/**
 * 版本轴的唯一控件，挂在侧边栏「建模」组里。
 *
 * 放在组里而不是每个页面顶部：它管的是整组页面看哪份数据，不是某一页的
 * 局部开关。跟着组一起收起也是对的——不在建模里的时候，这个轴没有意义。
 */
export function VersionSwitcher() {
  const [version, setVersion] = useOntologyVersion()
  const { tenantId } = useAdminTenant()

  // 换租户时回到草稿：带着上一个租户的"已确认版本"切过去，容易看着只读
  // 快照却以为在编辑草稿。跳过首次挂载——那不是切换，而且直接进来的
  // ?version=confirmed 链接会被这个 effect 当场清掉。
  const lastTenant = useRef(tenantId)
  useEffect(() => {
    if (lastTenant.current === tenantId) return
    lastTenant.current = tenantId
    setVersion('draft')
  }, [tenantId, setVersion])

  return (
    <div
      role="group"
      aria-label="本体版本"
      className="mx-2 flex overflow-hidden rounded-control border border-subtle"
    >
      <button
        type="button"
        aria-pressed={version === 'draft'}
        onClick={() => setVersion('draft')}
        className={segmentClass(version === 'draft')}
      >
        草稿
      </button>
      <button
        type="button"
        aria-pressed={version === 'confirmed'}
        onClick={() => setVersion('confirmed')}
        className={segmentClass(version === 'confirmed')}
      >
        已确认
      </button>
    </div>
  )
}
