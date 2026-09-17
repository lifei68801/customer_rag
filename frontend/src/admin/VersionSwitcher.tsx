import { useEffect, useRef } from 'react'
import { useAdminTenant } from './TenantContext'
import { useOntologyVersion } from './useOntologyVersion'

const segmentClass = (active: boolean) =>
  `min-h-[32px] flex-1 cursor-pointer px-2 text-xs font-bold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink ${
    active ? 'bg-ink text-paper' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

/**
 * 版本轴的控件，放在本体结构页和本体图页的页头。
 *
 * 曾经挂在侧边栏「建模」组里——理由是"它管的是整组页面"。实际用下来不成
 * 立：用户在本体结构页上看着一份只读快照，控件却在几十像素外的导航里，
 * 没人会往那儿找；而看哪一版是这一页最要紧的上下文，它该跟标题在一起。
 *
 * 两个页面各放一个实例，状态仍然只有一份（URL 上的 ?version=），所以在
 * 一边切完跳到另一边看到的还是同一版。
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
      className="flex flex-shrink-0 overflow-hidden rounded-control border border-subtle"
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
