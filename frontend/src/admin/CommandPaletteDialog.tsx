import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Command } from 'cmdk'
import { Building2, Gauge, Palette, type LucideIcon } from 'lucide-react'
import { useAdminAuth } from './useAdminAuth'
import { useAdminDensity } from './DensityContext'
import { useAdminSkin, type SkinId } from './SkinContext'
import { useAdminTenant } from './TenantContext'
import { adminFetch } from './adminApi'
import { NAV_GROUPS } from '../adminRoutes'

interface TenantOption {
  tenant_id: string
  display_name?: string
}

interface CommandItem {
  id: string
  label: string
  hint?: string
  icon: LucideIcon
  group: string
  run: () => void
}

/**
 * ⌘K 命令面板的内容部分。
 *
 * 单独成文件是为了让 cmdk（连同它的 Radix 依赖，实测主包 +56 kB）走按需
 * 加载——这个面板可能永远不被打开，不该让每个页面都为它付费。外层的
 * 快捷键监听在 CommandPaletteTrigger 里，只有几行、没有重依赖。
 *
 * 它不新增任何能力——跳页面、切租户、切密度、切皮肤这四类动作，各自的
 * context 方法本来就在（TenantContext / DensityContext / SkinContext），
 * 命令面板只是给它们一个统一入口。所以这个组件里没有任何自己的状态，
 * 只有对已有 context 的调用。
 *
 * 快捷键冲突：本项目已有两处全局键盘监听——ConfirmContext 的 Escape、
 * GraphReviewsPage 的 j/k/a/r/x 分诊。⌘K 带修饰键，跟前者不同键；后者
 * 明确排除了 metaKey/ctrlKey，天然让路。
 */
export function CommandPaletteDialog({ onClose }: { onClose: () => void }) {
  const [tenants, setTenants] = useState<TenantOption[]>([])
  const navigate = useNavigate()
  const { sessionToken } = useAdminAuth()
  const { tenantId, setTenantId } = useAdminTenant()
  const { density, setDensity } = useAdminDensity()
  const { skin, setSkin } = useAdminSkin()

  // 租户列表只在面板第一次打开时拉——它不是首屏需要的东西，没必要让每个
  // 页面都为一个可能永远不会被打开的面板付一次请求。
  useEffect(() => {
    if (!sessionToken || tenants.length > 0) return
    void (async () => {
      try {
        const response = await adminFetch('/api/admin/tenants', sessionToken)
        if (!response.ok) return
        const data = (await response.json()) as { tenants: TenantOption[] }
        setTenants(data.tenants)
      } catch {
        // 拉不到租户列表不该让整个面板失效——导航和外观命令仍然可用。
      }
    })()
  }, [sessionToken, tenants.length])

  const close = onClose
  const withClose = (fn: () => void) => () => {
    fn()
    close()
  }

  const items: CommandItem[] = [
    // 直接从侧边栏那张表生成，不再手写一份。手写的表不会在改路由时报错：
    // 上一版里它有「数据加工」「知识图谱审核」这些已经不存在的名字，缺三
    // 个目的地，还有一条指向一个从来没存在过的路径——点了只是白屏。
    ...NAV_GROUPS.flatMap((group) =>
      group.items.map((item) => ({
        id: `nav-${item.path}`,
        group: '导航',
        icon: item.icon,
        label: item.label,
        // 带上组名：光看「表格导入」不知道它属于哪一段流程，而搜索时输入
        // 「接入」也该能找到它。
        hint: group.label,
        run: withClose(() => navigate(item.path)),
      })),
    ),
    ...tenants.map((t) => ({
      id: `tenant-${t.tenant_id}`,
      group: '切换租户',
      icon: Building2,
      label: t.display_name ? `${t.display_name}（${t.tenant_id}）` : t.tenant_id,
      hint: t.tenant_id === tenantId ? '当前' : undefined,
      run: withClose(() => setTenantId(t.tenant_id)),
    })),
    {
      id: 'density-toggle', group: '外观', icon: Gauge,
      label: density === 'compact' ? '切换到标准密度' : '切换到紧凑密度',
      run: withClose(() => setDensity(density === 'compact' ? 'standard' : 'compact')),
    },
    ...(['default', 'dark', 'business-blue'] as SkinId[]).map((id) => ({
      id: `skin-${id}`,
      group: '外观',
      icon: Palette,
      label: `皮肤：${{ default: '默认', dark: '暗色', 'business-blue': '商务蓝' }[id]}`,
      hint: id === skin ? '当前' : undefined,
      run: withClose(() => setSkin(id)),
    })),
  ]

  const groups = [...new Set(items.map((item) => item.group))]

  return (
    <div
      // 一个覆盖全屏、抓走焦点的浮层必须有 dialog 语义，否则屏幕阅读器
      // 不知道自己进了模态，还在念下面那一层页面。
      role="dialog"
      aria-modal="true"
      aria-label="命令面板"
      className="fixed inset-0 z-[1000] flex items-start justify-center bg-black/50 p-4 pt-[12vh]"
      // 点遮罩关闭。面板本身 stopPropagation，避免点内容时误关。
      onClick={close}
    >
      <Command
        label="命令面板"
        loop
        className="w-full max-w-xl overflow-hidden rounded-modal border border-subtle bg-card shadow-lg"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') close()
        }}
      >
        <Command.Input
          autoFocus
          placeholder="跳转页面、切换租户或外观…"
          className="w-full border-b border-subtle bg-card px-4 py-3 text-ink outline-none placeholder:text-ink-soft"
        />
        <Command.List className="max-h-80 overflow-y-auto p-2">
          <Command.Empty className="px-3 py-6 text-center text-sm text-ink-soft">
            没有匹配的命令。
          </Command.Empty>
          {groups.map((group) => (
            <Command.Group
              key={group}
              heading={group}
              className="px-1 py-1 text-xs font-bold text-ink-soft [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1"
            >
              {items
                .filter((item) => item.group === group)
                .map((item) => (
                  <Command.Item
                    key={item.id}
                    value={`${item.group} ${item.label} ${item.hint ?? ''}`}
                    onSelect={item.run}
                    className="flex cursor-pointer items-center gap-2 rounded-control px-2 py-2 text-sm text-ink data-[selected=true]:bg-interactive-hover"
                  >
                    <item.icon aria-hidden="true" className="h-4 w-4 shrink-0 text-ink-soft" strokeWidth={1.5} />
                    <span className="flex-1">{item.label}</span>
                    {item.hint && <span className="text-xs text-ink-soft">{item.hint}</span>}
                  </Command.Item>
                ))}
            </Command.Group>
          ))}
        </Command.List>
      </Command>
    </div>
  )
}
