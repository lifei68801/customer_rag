import { Suspense, lazy, useEffect, useState } from 'react'

// cmdk 连同它的 Radix 依赖实测让主包 +56 kB，而这个面板可能永远不被打开。
// 只有真的按下 ⌘K 才加载对话框本体；这里留下的只有一个几行的键盘监听。
const CommandPaletteDialog = lazy(() =>
  import('./CommandPaletteDialog').then((m) => ({ default: m.CommandPaletteDialog })),
)

/**
 * ⌘K 命令面板的触发器。
 *
 * 它不新增任何能力——跳页面、切租户、切密度、切皮肤这四类动作，各自的
 * context 方法本来就在，命令面板只是给它们一个统一入口。
 *
 * 快捷键冲突：本项目已有两处全局键盘监听——ConfirmContext 的 Escape、
 * GraphReviewsPage 的 j/k/a/r/x 分诊。⌘K 带修饰键，跟前者不同键；后者
 * 明确排除了 metaKey/ctrlKey，天然让路。
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'k' && (event.metaKey || event.ctrlKey)) {
        event.preventDefault()
        setOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  if (!open) return null

  // fallback 为 null：加载对话框只需要几十毫秒，闪一个骨架屏比什么都不显示
  // 更打扰——用户刚按下快捷键，期待的是面板出现，不是加载指示器。
  return (
    <Suspense fallback={null}>
      <CommandPaletteDialog onClose={() => setOpen(false)} />
    </Suspense>
  )
}
