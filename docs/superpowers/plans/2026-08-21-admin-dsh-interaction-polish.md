# 后台交互打磨（参考 DSH）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给管理后台补上 5 类此前缺失的交互反馈——瞬时 toast、骨架屏加载态、活跃任务脉冲提示、可切换的列表密度、图标按钮 tooltip，并顺手改造 7 处空状态文案、修复一处遗留的原生 `window.confirm()`。

**Architecture:** 5 个新共享组件（`ToastContext`、`Skeleton`、`Tooltip`、`DensityContext`+`DensitySwitcher`，加上 `TaskStatusBadge` 的小改）+ 6 个消费页面/组件的逐点接入。`ToastProvider`/`ConfirmProvider` 提升到 `main.tsx` 站点级根节点（前台聊天页也要用），`DensityProvider` 只挂在 `AdminLayout.tsx`（只有后台列表用得到）。

**Tech Stack:** React + TypeScript + Tailwind（Tailwind 内置 `animate-pulse` 工具类，不需要自定义 keyframe）。项目无自动化前端测试框架，验证手段是 `npx tsc --noEmit` + 手工浏览器验证。

**Spec:** `docs/superpowers/specs/2026-08-21-admin-dsh-interaction-polish.md`

## Global Constraints

- 新粗野主义视觉语言：0 圆角、`border-2 border-ink` 实边、`shadow-brutal`/`shadow-brutal-sm` 硬直角偏移阴影。禁止引入圆角、柔和阴影、细边框。
- 所有新动画必须遵守 `prefers-reduced-motion`（用 Tailwind 的 `motion-reduce:` 前缀）。
- Toast：`bg-ink text-paper border-2 border-ink shadow-brutal`，顶部居中 `fixed top-4 left-1/2 -translate-x-1/2 z-50`，`pointer-events-none`，0.15s 淡入 → 停留 3s → 0.15s 淡出，新的直接覆盖旧的（不排队），无手动关闭按钮，`role="status" aria-live="polite"`。
- Toast 只用于成功确认，阻断性错误（`role="alert"`）保持原样不动，不挪去 toast。
- 骨架屏用方块占位 + Tailwind 内置 `animate-pulse`，颜色 `bg-ink-soft/40`，不用圆形旋转 spinner。
- `ConfirmProvider`/`ToastProvider`/`SkinProvider` 都挂在 `main.tsx` 根节点（站点级，前台+后台共用）；`TenantProvider`/`DensityProvider` 只挂在 `AdminLayout.tsx`（后台专属）。
- 项目校验方式：每个任务完成后运行 `cd frontend && npx tsc --noEmit`，必须无输出（无类型错误）。改动只涉及 `tailwind.config.ts` 时才需要完整重启 Vite dev server（杀进程 + 清 `node_modules/.vite` 缓存 + 重启）；纯 `.tsx`/`.css` 内容改动可以依赖热更新，本计划所有任务都不改 `tailwind.config.ts`，因此不需要重启。

---

### Task 1: 站点级 Toast + Confirm 提升

**Files:**
- Create: `frontend/src/admin/ToastContext.tsx`
- Modify: `frontend/src/main.tsx`（全量替换）
- Modify: `frontend/src/admin/AdminLayout.tsx:1-6, 32-34, 70-71`（移除 `ConfirmProvider` 的挂载，因为已经提升到 main.tsx）

**Interfaces:**
- Produces: `useToast(): (message: string) => void`（Task 6-11 消费），`ToastProvider`（组件，本任务里挂到 `main.tsx`）

- [ ] **Step 1: 创建 `frontend/src/admin/ToastContext.tsx`**

```tsx
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'

type ToastFn = (message: string) => void

const ToastContext = createContext<ToastFn | null>(null)

/**
 * 站点级瞬时成功反馈——用于替代"插入后不会消失、还会顶开布局"的常驻
 * 确认文字，或者原本完全没有反馈的操作（删除、上传等）。跟 ConfirmContext
 * 一样用 Context + Provider 模式，挂载在 main.tsx 的根节点（前台聊天页和
 * 后台管理共用）。只用于"操作成功"这类确认性反馈，阻断性错误仍然留在
 * 原地（role="alert"），不挪到这里；不支持多条堆叠、不支持手动关闭。
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState<string | null>(null)
  const [visible, setVisible] = useState(false)
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showToast = useCallback<ToastFn>((next) => {
    if (hideTimerRef.current) clearTimeout(hideTimerRef.current)
    if (clearTimerRef.current) clearTimeout(clearTimerRef.current)
    setMessage(next)
    setVisible(true)
    hideTimerRef.current = setTimeout(() => {
      setVisible(false)
      clearTimerRef.current = setTimeout(() => setMessage(null), 150)
    }, 3000)
  }, [])

  useEffect(() => {
    return () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current)
      if (clearTimerRef.current) clearTimeout(clearTimerRef.current)
    }
  }, [])

  return (
    <ToastContext.Provider value={showToast}>
      {children}
      {message && (
        <div
          role="status"
          aria-live="polite"
          className={`pointer-events-none fixed left-1/2 top-4 z-50 -translate-x-1/2 border-2 border-ink bg-ink px-4 py-2 text-sm font-bold text-paper shadow-brutal transition-opacity duration-150 motion-reduce:transition-none ${
            visible ? 'opacity-100' : 'opacity-0'
          }`}
        >
          {message}
        </div>
      )}
    </ToastContext.Provider>
  )
}

export function useToast(): ToastFn {
  const value = useContext(ToastContext)
  if (value === null) {
    throw new Error('useToast() 必须在 <ToastProvider> 内部使用')
  }
  return value
}
```

- [ ] **Step 2: 把 `frontend/src/main.tsx` 全量替换成**

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { SkinProvider } from './admin/SkinContext'
import { ConfirmProvider } from './admin/ConfirmContext'
import { ToastProvider } from './admin/ToastContext'
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/space-mono/400.css'
import '@fontsource/space-mono/700.css'
import './styles/index.css'
import 'katex/dist/katex.min.css'

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('未找到 #root 挂载节点')
}

// SkinProvider/ConfirmProvider/ToastProvider 都包在最外层（而不是只包
// AdminLayout）——这三个都是站点级偏好/能力，前台聊天页和后台管理共用。
// 之前 ConfirmProvider 只在 AdminLayout 里挂载过，导致 ChatSidebar.tsx
// 拿不到 useConfirm()，只能退回原生 window.confirm()；ToastProvider 从
// 一开始就直接挂在这里，避免重蹈覆辙。
createRoot(rootElement).render(
  <StrictMode>
    <SkinProvider>
      <ConfirmProvider>
        <ToastProvider>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </ToastProvider>
      </ConfirmProvider>
    </SkinProvider>
  </StrictMode>,
)
```

- [ ] **Step 3: 修改 `frontend/src/admin/AdminLayout.tsx`——移除重复挂载的 `ConfirmProvider`**

把文件顶部的 import 从：

```tsx
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { ConfirmProvider } from './ConfirmContext'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
```

改成：

```tsx
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
```

把注释和 return 语句从：

```tsx
  // SkinProvider 现在挂载在 main.tsx 的根节点（站点级偏好，前台/后台共用），
  // 这里不再重复包一层。
  return (
    <TenantProvider>
      <ConfirmProvider>
        <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
```

改成：

```tsx
  // SkinProvider/ConfirmProvider 现在都挂载在 main.tsx 的根节点（站点级
  // 能力，前台/后台共用），这里不再重复包一层。
  return (
    <TenantProvider>
      <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
```

文件末尾从：

```tsx
          </main>
        </div>
      </ConfirmProvider>
    </TenantProvider>
  )
}
```

改成：

```tsx
          </main>
        </div>
    </TenantProvider>
  )
}
```

（注意：这一步会让 `<div>` 少一层缩进但不用重新格式化整个文件——保留原有缩进层级即可，只删掉 `<ConfirmProvider>`/`</ConfirmProvider>` 这两行，TypeScript/JSX 不关心缩进对不对齐。）

- [ ] **Step 4: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（无类型错误）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/ToastContext.tsx frontend/src/main.tsx frontend/src/admin/AdminLayout.tsx
git commit -m "feat(admin): promote ConfirmProvider site-wide, add ToastProvider"
```

---

### Task 2: Skeleton 骨架屏组件

**Files:**
- Create: `frontend/src/admin/Skeleton.tsx`

**Interfaces:**
- Produces: `<Skeleton variant="table-rows" | "card-list" count?={number} />`（Task 6-9 消费）

- [ ] **Step 1: 创建 `frontend/src/admin/Skeleton.tsx`**

```tsx
interface SkeletonProps {
  variant: 'table-rows' | 'card-list'
  count?: number
}

const ROW_WIDTHS = ['60%', '35%', '45%']
const CARD_WIDTHS = ['55%', '30%']

/**
 * 加载态占位——用方块骨架而不是纯文字"加载中…"，让加载前后的高度基本
 * 一致，避免内容到达时整块跳动（CLS）。不用 DSH 的圆形旋转 spinner：
 * 圆形旋转跟这个项目 0 圆角/实边的新粗野主义语言冲突，改用方块 +
 * Tailwind 内置 animate-pulse（透明度 1↔0.5 循环）。
 */
export function Skeleton({ variant, count = 3 }: SkeletonProps) {
  if (variant === 'table-rows') {
    return (
      <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm" aria-hidden="true">
        {Array.from({ length: count }, (_, row) => (
          <div key={row} className="flex items-center gap-4 border-b border-ink/20 px-3 py-2 last:border-b-0">
            {ROW_WIDTHS.map((width, col) => (
              <div key={col} className="h-4 animate-pulse bg-ink-soft/40" style={{ width }} />
            ))}
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2" aria-hidden="true">
      {Array.from({ length: count }, (_, card) => (
        <div key={card} className="flex flex-col gap-2 border-2 border-ink bg-card p-4 shadow-brutal-sm">
          {CARD_WIDTHS.map((width, line) => (
            <div key={line} className="h-4 animate-pulse bg-ink-soft/40" style={{ width }} />
          ))}
        </div>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add frontend/src/admin/Skeleton.tsx
git commit -m "feat(admin): add Skeleton loading-placeholder component"
```

---

### Task 3: TaskStatusBadge 活跃脉冲

**Files:**
- Modify: `frontend/src/admin/TaskStatusBadge.tsx`（全量替换）

**Interfaces:**
- Consumes: 无新依赖
- Produces: `<TaskStatusBadge tone="active" .../>` 现在会在文字前渲染一个闪烁小方块，其余 4 种 tone 不受影响，组件对外签名不变

- [ ] **Step 1: 把 `frontend/src/admin/TaskStatusBadge.tsx` 全量替换成**

```tsx
type BadgeTone = 'neutral' | 'active' | 'success' | 'error' | 'warning'

interface TaskStatusBadgeProps {
  tone: BadgeTone
  label: string
}

// 调用方负责把自己领域里的原始状态字符串（'running'/'pending'/'approved' 等，
// 每个页面的取值集合都不一样）映射成这四种统一的语气 + 展示文案，这个组件
// 本身不认识任何具体业务状态值，只负责统一视觉呈现。
const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: 'border-ink bg-paper text-ink',
  active: 'border-ink bg-accent-cyan text-ink',
  success: 'border-status-success bg-paper text-status-success',
  error: 'border-status-error bg-paper text-status-error',
  warning: 'border-ink bg-accent-yellow text-ink',
}

export function TaskStatusBadge({ tone, label }: TaskStatusBadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 border-2 px-2 py-0.5 text-xs font-bold ${TONE_CLASS[tone]}`}
    >
      {tone === 'active' && (
        <span
          aria-hidden="true"
          className="h-2 w-2 flex-shrink-0 animate-pulse bg-ink motion-reduce:animate-none"
        />
      )}
      {label}
    </span>
  )
}
```

- [ ] **Step 2: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 3: Commit**

```bash
git add frontend/src/admin/TaskStatusBadge.tsx
git commit -m "feat(admin): pulse indicator dot on active task badges"
```

---

### Task 4: Tooltip 组件 + 接入 Pager

**Files:**
- Create: `frontend/src/admin/Tooltip.tsx`
- Modify: `frontend/src/admin/Pager.tsx`

**Interfaces:**
- Produces: `<Tooltip label={string}>{children}</Tooltip>`（本任务里接入 Pager，Task 11 里接入 ChatSidebar）

- [ ] **Step 1: 创建 `frontend/src/admin/Tooltip.tsx`**

```tsx
import { useEffect, useRef, useState, type ReactNode } from 'react'

interface TooltipProps {
  label: string
  children: ReactNode
}

const SHOW_DELAY_MS = 150

/**
 * 固定在子元素正上方展开的提示——只用在全项目仅有的 2 处纯图标控件
 * （没有可见文字，只能靠这个补充视觉提示）：ChatSidebar 的删除会话
 * 图标、Pager 的上一页/下一页箭头。不做防溢出智能定位：这 2 处控件
 * 位置固定，不存在被视口边缘遮挡的情况。子元素自身的 aria-label 不受
 * 影响，两者并存：aria-label 给屏幕阅读器，这个提示给视觉用户。
 *
 * mounted 控制"延迟 150ms 后要不要渲染这个提示"，visible 控制"渲染出来
 * 之后的下一帧再把透明度拉到 100%"——拆成两个状态是为了让 CSS
 * transition 真正播放：如果一次性用 opacity-100 挂载，浏览器不会补一帧
 * opacity-0 的初始状态，transition 就没有起点可过渡，直接瞬间出现。
 */
export function Tooltip({ label, children }: TooltipProps) {
  const [mounted, setMounted] = useState(false)
  const [visible, setVisible] = useState(false)
  const showTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!mounted) return
    const frame = requestAnimationFrame(() => setVisible(true))
    return () => cancelAnimationFrame(frame)
  }, [mounted])

  const show = () => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current)
    showTimerRef.current = setTimeout(() => setMounted(true), SHOW_DELAY_MS)
  }

  const hide = () => {
    if (showTimerRef.current) clearTimeout(showTimerRef.current)
    setMounted(false)
    setVisible(false)
  }

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {mounted && (
        <span
          role="tooltip"
          className={`pointer-events-none absolute bottom-full left-1/2 z-40 mb-1.5 -translate-x-1/2 whitespace-nowrap border-2 border-ink bg-ink px-2 py-1 text-xs font-bold text-paper shadow-brutal-sm transition-opacity duration-150 motion-reduce:transition-none ${
            visible ? 'opacity-100' : 'opacity-0'
          }`}
        >
          {label}
        </span>
      )}
    </span>
  )
}
```

- [ ] **Step 2: 接入 `frontend/src/admin/Pager.tsx`**

当前文件顶部只有：

```tsx
```

（`Pager.tsx` 没有任何 import——`getPageNumbers`/`pageButtonClass` 都是本文件内定义的纯函数。）在文件最顶部加一行：

```tsx
import { Tooltip } from './Tooltip'
```

把"上一页"按钮从：

```tsx
      <button
        type="button"
        onClick={() => onPageChange(page - 1)}
        disabled={page <= 1}
        aria-label="上一页"
        className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
      >
        ‹
      </button>
```

改成：

```tsx
      <Tooltip label="上一页">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          aria-label="上一页"
          className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
        >
          ‹
        </button>
      </Tooltip>
```

把"下一页"按钮从：

```tsx
      <button
        type="button"
        onClick={() => onPageChange(page + 1)}
        disabled={page >= totalPages}
        aria-label="下一页"
        className={`${pageButtonClass(false)} disabled:cursor-not-allowed disabled:opacity-50`}
```

（后面跟着 `>` 和 `›` 内容，保持不动）改成用同样的方式包一层 `<Tooltip label="下一页">`。

- [ ] **Step 3: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/Tooltip.tsx frontend/src/admin/Pager.tsx
git commit -m "feat(admin): add Tooltip component, wire into Pager prev/next"
```

---

### Task 5: 列表密度切换（DensityContext + DensitySwitcher）

**Files:**
- Create: `frontend/src/admin/DensityContext.tsx`
- Create: `frontend/src/admin/DensitySwitcher.tsx`
- Modify: `frontend/src/admin/AdminLayout.tsx`

**Interfaces:**
- Produces: `useAdminDensity(): { density: 'standard' | 'compact'; setDensity: (next) => void }`（Task 6-9 消费）

- [ ] **Step 1: 创建 `frontend/src/admin/DensityContext.tsx`**

```tsx
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

const DENSITY_STORAGE_KEY = 'admin_density'

export type DensityId = 'standard' | 'compact'

const VALID_DENSITY_IDS: readonly DensityId[] = ['standard', 'compact']

function isDensityId(value: string | null): value is DensityId {
  return value !== null && (VALID_DENSITY_IDS as readonly string[]).includes(value)
}

interface DensityContextValue {
  density: DensityId
  setDensity: (next: DensityId) => void
}

const DensityContext = createContext<DensityContextValue | null>(null)

/**
 * 列表/表格密度偏好——管理员个人偏好，存 localStorage，架构照抄
 * SkinContext。跟皮肤不同的是：间距是 Tailwind 静态类名，不是能用
 * CSS 变量驱动的颜色，所以各消费组件要自己读 useAdminDensity() 后
 * 二选一 className；同步到 <html data-density> 的这个属性只作为可选
 * 的 CSS hook 保留，不强制要求消费组件用它。只挂在 AdminLayout 里
 * （不像 SkinProvider 要提升到 main.tsx）：密度只影响后台列表，前台
 * 聊天页的会话列表不在这次改造范围内。
 */
export function DensityProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<DensityId>(() => {
    const stored = localStorage.getItem(DENSITY_STORAGE_KEY)
    return isDensityId(stored) ? stored : 'standard'
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-density', density)
  }, [density])

  const value = useMemo<DensityContextValue>(
    () => ({
      density,
      setDensity: (next: DensityId) => {
        localStorage.setItem(DENSITY_STORAGE_KEY, next)
        setDensityState(next)
      },
    }),
    [density],
  )

  return <DensityContext.Provider value={value}>{children}</DensityContext.Provider>
}

export function useAdminDensity(): DensityContextValue {
  const value = useContext(DensityContext)
  if (value === null) {
    throw new Error('useAdminDensity() 必须在 <DensityProvider> 内部使用')
  }
  return value
}
```

- [ ] **Step 2: 创建 `frontend/src/admin/DensitySwitcher.tsx`**

```tsx
import { useAdminDensity, type DensityId } from './DensityContext'

const DENSITY_OPTIONS: { id: DensityId; label: string }[] = [
  { id: 'standard', label: '标准' },
  { id: 'compact', label: '紧凑' },
]

export function DensitySwitcher() {
  const { density, setDensity } = useAdminDensity()

  return (
    <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
      列表密度
      <select
        value={density}
        onChange={(event) => setDensity(event.target.value as DensityId)}
        aria-label="切换列表密度"
        className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
      >
        {DENSITY_OPTIONS.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
```

- [ ] **Step 3: 接入 `frontend/src/admin/AdminLayout.tsx`**

顶部 import 从（Task 1 完成后的状态）：

```tsx
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
```

改成：

```tsx
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { DensityProvider } from './DensityContext'
import { DensitySwitcher } from './DensitySwitcher'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
```

`return` 语句从：

```tsx
  return (
    <TenantProvider>
      <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
```

改成：

```tsx
  return (
    <TenantProvider>
      <DensityProvider>
        <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
```

侧边栏里 `<SkinSwitcher />` 紧跟着加一行：

```tsx
              <TenantSwitcher />
              <SkinSwitcher />
              <DensitySwitcher />
```

文件末尾从：

```tsx
          </main>
        </div>
    </TenantProvider>
  )
}
```

改成：

```tsx
          </main>
        </div>
      </DensityProvider>
    </TenantProvider>
  )
}
```

- [ ] **Step 4: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/DensityContext.tsx frontend/src/admin/DensitySwitcher.tsx frontend/src/admin/AdminLayout.tsx
git commit -m "feat(admin): add list-density preference (standard/compact)"
```

---

### Task 6: OntologySchemaPage.tsx —— toast、空状态、骨架屏、密度

这是改动最多的文件：一个主组件（页面级确认操作）+ 3 个子组件（`TermTypesTab`/`RelationTypesTab`/`ConstraintsTab`，各自独立管理增删改）。四处都要接入 `useToast()`；`useConfirm()` 已经在用，不用改。

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`

**Interfaces:**
- Consumes: `useToast()` from `./ToastContext`（Task 1），`Skeleton` from `./Skeleton`（Task 2），`useAdminDensity()` from `./DensityContext`（Task 5）

- [ ] **Step 1: 顶部 import 加一行**

文件第 1-5 行从：

```tsx
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminTenant } from './TenantContext'
```

改成：

```tsx
import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
```

- [ ] **Step 2: 主组件 `handleConfirm`（第 185-215 行附近）加 toast**

在 `await refreshStatus()` 那一行**之前**（函数顶部）先拿到 `showToast`——找到主组件函数体开头已有 `const confirm = useConfirm()` 那一行（如果没有就是别的 hook 调用），在旁边加一行 `const showToast = useToast()`。

`handleConfirm` 的 try 块从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '确认失败'))
      }
      await refreshStatus()
      setConfirmVersion((v) => v + 1)
```

改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '确认失败'))
      }
      showToast('已确认')
      await refreshStatus()
      setConfirmVersion((v) => v + 1)
```

- [ ] **Step 3: `TermTypesTab` 组件——加 `useToast()`，`handleDelete`/`handleMigrate` 加 toast，空状态改文案，加载态换 Skeleton，表格加密度**

`TermTypesTab` 组件函数体开头（`const confirm = useConfirm()` 附近）加一行：

```tsx
  const showToast = useToast()
  const { density } = useAdminDensity()
```

`handleDelete`（第 487-509 行附近）的成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除实体类型失败'))
      }
      await refresh()
      onDataChanged()
```

（这是 `handleDelete` 里的那一段，不是 `handleMigrate` 里同样结构的那段）改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除实体类型失败'))
      }
      showToast('已删除实体类型')
      await refresh()
      onDataChanged()
```

`handleMigrate`（第 511-546 行附近）里，把原本设置 `migrateSuccessMessage` 常驻文字的那一行：

```tsx
      const data = (await response.json()) as { terms_migrated: number; graph_nodes_migrated: number }
      setMigrateSuccessMessage(`已迁移 ${data.terms_migrated} 条术语、${data.graph_nodes_migrated} 个图谱节点`)
      setMigratingFrom(null)
      setMigrateTarget('')
```

改成：

```tsx
      const data = (await response.json()) as { terms_migrated: number; graph_nodes_migrated: number }
      showToast(`已迁移 ${data.terms_migrated} 条术语、${data.graph_nodes_migrated} 个图谱节点`)
      setMigratingFrom(null)
      setMigrateTarget('')
```

由于 `migrateSuccessMessage` 这个 state 不再被设置为非空值，整段渲染它的 JSX（`{migrateSuccessMessage && <p className="text-sm text-ink">{migrateSuccessMessage}</p>}`，在第 700 行附近）连同 `const [migrateSuccessMessage, setMigrateSuccessMessage] = useState<string | null>(null)` 声明（第 389 行附近）、以及另外两处把它设回 `null` 的调用（第 585、738 行附近，`setMigrateSuccessMessage(null)`）全部删掉——`noUnusedLocals` 会在留有死代码时报错。

空状态（第 552 行）从：

```tsx
      {loaded && items.length === 0 && editingValue === null && (
        <p className="text-ink-soft">还没有任何{view === 'draft' ? '草稿' : '已确认的'}实体类型。</p>
      )}
```

改成：

```tsx
      {loaded && items.length === 0 && editingValue === null && (
        <p className="text-ink-soft">
          还没有任何{view === 'draft' ? '草稿' : '已确认的'}实体类型。
          {view === 'draft' && '点击下方「+ 新增实体类型」创建一个。'}
        </p>
      )}
```

加载态（第 550 行）从：

```tsx
      {!loaded && <p className="text-ink-soft">加载中…</p>}
```

改成：

```tsx
      {!loaded && <Skeleton variant="table-rows" count={4} />}
```

表格单元格密度：在 `return (` 之前（组件函数体内）加一行

```tsx
  const cellPadding = density === 'compact' ? 'px-2 py-1' : 'px-3 py-2'
```

第 555-606 行的表格从：

```tsx
      {items.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className="px-3 py-2">类型名</th>
                <th className="px-3 py-2">属性字段数</th>
                {view === 'draft' && <th className="px-3 py-2">操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.value} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className="px-3 py-2">{item.value}</td>
                  <td className="px-3 py-2">{item.extra_fields.length}</td>
                  {view === 'draft' && (
                    <td className="px-3 py-2">
```

改成：

```tsx
      {items.length > 0 && (
        <div className="overflow-x-auto border-2 border-ink bg-card shadow-brutal-sm">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b-2 border-ink bg-paper text-ink">
                <th className={cellPadding}>类型名</th>
                <th className={cellPadding}>属性字段数</th>
                {view === 'draft' && <th className={cellPadding}>操作</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.value} className="border-b border-ink/20 text-ink last:border-b-0">
                  <td className={cellPadding}>{item.value}</td>
                  <td className={cellPadding}>{item.extra_fields.length}</td>
                  {view === 'draft' && (
                    <td className={cellPadding}>
```

（表格剩余部分——`<td>` 内的按钮和它的收尾标签——保持不动，只有上面这几处 `px-3 py-2` 字面量换成 `{cellPadding}`。）

- [ ] **Step 4: `RelationTypesTab` 组件——同款改法**

按 Step 3 的模式对 `RelationTypesTab` 组件做同样的四处改动：

1. 组件顶部加 `const showToast = useToast()` 和 `const { density } = useAdminDensity()`
2. `handleDelete`（第 866-888 行附近）成功分支加 `showToast('已删除关系类型')`
3. `handleMigrate`（第 890 行开始）里原本 `setMigrateSuccessMessage(\`已迁移 ${data.migrated_count} 条边\`)` 那一行（第 917 行附近）改成 `showToast(\`已迁移 ${data.migrated_count} 条边\`)`，同样删掉这个组件自己的 `migrateSuccessMessage` state 声明（第 781 行附近）、渲染它的 JSX（第 1063 行附近）、以及另一处 `setMigrateSuccessMessage(null)`（第 966 行附近）
4. 空状态（第 930 行）从

```tsx
      {loaded && items.length === 0 && <p className="text-ink-soft">还没有任何{view === 'draft' ? '草稿' : '已确认的'}关系类型。</p>}
```

改成：

```tsx
      {loaded && items.length === 0 && (
        <p className="text-ink-soft">
          还没有任何{view === 'draft' ? '草稿' : '已确认的'}关系类型。
          {view === 'draft' && '点击下方「+ 新增关系类型」创建一个。'}
        </p>
      )}
```

5. 加载态（第 929 行）`<p className="text-ink-soft">加载中…</p>` 改成 `<Skeleton variant="table-rows" count={4} />`
6. 表格（第 931 行开始）用同样的 `cellPadding` 模式接入密度

- [ ] **Step 5: `ConstraintsTab` 组件——同款改法（注意提示方位是"下方"不是"上方"，因为约束的新增表单本来就在列表下面）**

1. 组件顶部加 `const showToast = useToast()` 和 `const { density } = useAdminDensity()`
2. `handleAdd`（第 1190-1223 行附近）成功分支，把

```tsx
      setSubject('')
      setRelationType('')
      setObject('')
      await refresh()
      onDataChanged()
```

改成：

```tsx
      showToast('已添加约束')
      setSubject('')
      setRelationType('')
      setObject('')
      await refresh()
      onDataChanged()
```

3. `handleRemove`（第 1225-1252 行）成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除约束失败'))
      }
      await refresh()
      onDataChanged()
```

改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除约束失败'))
      }
      showToast('已删除约束')
      await refresh()
      onDataChanged()
```
4. 空状态（第 1257-1259 行）从

```tsx
      {loaded && constraints.length === 0 && (
        <p className="text-ink-soft">还没有任何{view === 'draft' ? '草稿' : '已确认的'}约束。</p>
      )}
```

改成：

```tsx
      {loaded && constraints.length === 0 && (
        <p className="text-ink-soft">
          还没有任何{view === 'draft' ? '草稿' : '已确认的'}约束。
          {view === 'draft' && '在下方表单里添加一个。'}
        </p>
      )}
```

5. 加载态（第 1256 行）`<p className="text-ink-soft">加载中…</p>` 改成 `<Skeleton variant="table-rows" count={3} />`
6. 表格（第 1260 行开始）用同样的 `cellPadding` 模式接入密度

- [ ] **Step 6: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出——特别注意 `migrateSuccessMessage` 相关的 state/JSX/清空调用是否都删干净了（`noUnusedLocals` 开着，留一处没删就会报错）

- [ ] **Step 7: Commit**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx
git commit -m "feat(admin): toast/empty-state/skeleton/density for ontology schema tabs"
```

---

### Task 7: DocumentsPage.tsx —— toast、空状态链接、骨架屏、密度

**Files:**
- Modify: `frontend/src/admin/DocumentsPage.tsx`

**Interfaces:**
- Consumes: `useToast()`（Task 1），`Skeleton`（Task 2），`useAdminDensity()`（Task 5），`Link` from `react-router-dom`

- [ ] **Step 1: 顶部 import**

第 1-7 行从：

```tsx
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'
import { TaskStatusBadge } from './TaskStatusBadge'
```

改成：

```tsx
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { TaskStatusBadge } from './TaskStatusBadge'
import { useToast } from './ToastContext'
import { Pager } from './Pager'
```

组件函数体开头（`const confirm = useConfirm()` 附近）加：

```tsx
  const showToast = useToast()
  const { density } = useAdminDensity()
```

- [ ] **Step 2: 四处 toast**

`handleUpload`（第 138-172 行）成功分支从：

```tsx
      form.reset()
      // 复选框是 React 受控组件，form.reset() 只重置原生 file input，
      // 不重置这个状态——不手动清掉的话下一次上传会在不知情的情况下
      // 继续带着"构建图谱"参数提交。
      setBuildGraph(false)
      await pollNowRef.current()
```

改成：

```tsx
      showToast('已提交上传')
      form.reset()
      // 复选框是 React 受控组件，form.reset() 只重置原生 file input，
      // 不重置这个状态——不手动清掉的话下一次上传会在不知情的情况下
      // 继续带着"构建图谱"参数提交。
      setBuildGraph(false)
      await pollNowRef.current()
```

`handleDelete`（第 174-197 行）成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      await pollNowRef.current()
```

（这是 `handleDelete` 里那一段，注意跟 `handleDeleteJob` 区分）改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      showToast('已删除文档')
      await pollNowRef.current()
```

`handleRetryJob`（第 199-219 行）成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重试失败'))
      }
      await pollNowRef.current()
```

改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '重试失败'))
      }
      showToast('已重新提交')
      await pollNowRef.current()
```

`handleDeleteJob`（第 221-247 行）成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setJobActionId(null)
    }
  }
```

（这是 `handleDeleteJob` 里的错误消息也是"删除失败"的那一段——跟 `handleDelete` 的错误文案恰好相同，靠函数名和上下文区分，不要改错函数）改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '删除失败'))
      }
      showToast('已删除任务')
      await pollNowRef.current()
    } catch (err) {
      setJobError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setJobActionId(null)
    }
  }
```

- [ ] **Step 3: 空状态改真链接**

第 527-529 行从：

```tsx
        {loaded && documents.length === 0 && (
          <p className="text-ink-soft">当前租户还没有已摄取的文档。</p>
        )}
```

改成：

```tsx
        {loaded && documents.length === 0 && (
          <p className="text-ink-soft">
            当前租户还没有已摄取的文档。去
            <Link to="/admin/data-entry" className="font-bold underline">
              数据加工
            </Link>
            上传一份试试。
          </p>
        )}
```

- [ ] **Step 4: 加载态换 Skeleton**

第 451 行 `{!loaded && <p className="text-ink-soft">加载中…</p>}` 改成 `{!loaded && <Skeleton variant="card-list" count={3} />}`

- [ ] **Step 5: 文档列表卡片接入密度**

第 464-467 行从：

```tsx
              <div
                key={doc.file_path}
                className="flex flex-col gap-2 border-2 border-ink bg-card px-4 py-3 shadow-brutal-sm"
              >
```

改成：

```tsx
              <div
                key={doc.file_path}
                className={`flex flex-col gap-2 border-2 border-ink bg-card shadow-brutal-sm ${
                  density === 'compact' ? 'px-2.5 py-1.5' : 'px-4 py-3'
                }`}
              >
```

- [ ] **Step 6: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
git add frontend/src/admin/DocumentsPage.tsx
git commit -m "feat(admin): toast/empty-state-link/skeleton/density for DocumentsPage"
```

---

### Task 8: GraphReviewsPage.tsx —— toast、空状态改写、骨架屏、密度

**Files:**
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`

**Interfaces:**
- Consumes: `useToast()`（Task 1），`Skeleton`（Task 2），`useAdminDensity()`（Task 5）

- [ ] **Step 1: 顶部 import**

第 1-8 行从：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { Pager } from './Pager'
import { StandardNameInput } from './StandardNameInput'
import { TaskStatusBadge } from './TaskStatusBadge'
import { fetchGraphTerms, createTerm, type GraphTerm } from './termsApi'
```

改成：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { useAdminAuth } from './useAdminAuth'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { Pager } from './Pager'
import { StandardNameInput } from './StandardNameInput'
import { TaskStatusBadge } from './TaskStatusBadge'
import { fetchGraphTerms, createTerm, type GraphTerm } from './termsApi'
```

组件函数体开头加：

```tsx
  const showToast = useToast()
  const { density } = useAdminDensity()
```

- [ ] **Step 2: 五处 toast**

`handleApprove`（第 263-292 行）成功分支从：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '批准失败'))
      }
      await refreshPending()
```

（这是 `handleApprove` 里的那一段）改成：

```tsx
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '批准失败'))
      }
      showToast('已批准')
      await refreshPending()
```

`handleReject`（第 294-320 行）成功分支从：

```tsx
      setRejectNotes((prev) => {
        const next = { ...prev }
        delete next[reviewId]
        return next
      })
      await refreshPending()
```

改成：

```tsx
      showToast('已驳回')
      setRejectNotes((prev) => {
        const next = { ...prev }
        delete next[reviewId]
        return next
      })
      await refreshPending()
```

`handleBatchApprove`（第 322-360 行）末尾从：

```tsx
    setBatchResult({ success, failures })
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleBatchReject = async () => {
```

（这是 `handleBatchApprove` 结尾，紧接着就是 `handleBatchReject` 声明，用这个上下文精确定位）改成：

```tsx
    setBatchResult({ success, failures })
    if (success > 0) showToast(`已批准 ${success} 条`)
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleBatchReject = async () => {
```

`handleBatchReject`（第 362-397 行）末尾从：

```tsx
    setBatchResult({ success, failures })
    setBatchRejectNote('')
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleOpenCreateEntity = (
```

改成：

```tsx
    setBatchResult({ success, failures })
    if (success > 0) showToast(`已驳回 ${success} 条`)
    setBatchRejectNote('')
    setBatchProcessing(false)
    await refreshPending()
  }

  const handleOpenCreateEntity = (
```

`handleSubmitCreateEntity`（第 422-464 行）成功分支从：

```tsx
      setCreateDraft(null)
      // 创建成功后立即重新拉取本页 graphTerms，让同页其它引用同一新实体
      // 的候选行也能立刻搜到它——见 spec 决策 A.4。
      const refreshedTerms = await fetchGraphTerms(sessionToken, tenantId)
      setGraphTerms(refreshedTerms)
```

改成：

```tsx
      showToast('已创建实体候选')
      setCreateDraft(null)
      // 创建成功后立即重新拉取本页 graphTerms，让同页其它引用同一新实体
      // 的候选行也能立刻搜到它——见 spec 决策 A.4。
      const refreshedTerms = await fetchGraphTerms(sessionToken, tenantId)
      setGraphTerms(refreshedTerms)
```

- [ ] **Step 3: 历史空状态只改文案**

第 856 行从：

```tsx
        <p className="text-ink-soft">还没有处理过的记录。</p>
```

改成：

```tsx
        <p className="text-ink-soft">还没有处理过的记录——批准或驳回的候选会出现在这里。</p>
```

- [ ] **Step 4: 加载态换 Skeleton（两处）**

第 614 行（pending tab）：`{tab === 'pending' && !pendingLoaded && <p className="text-ink-soft">加载中…</p>}` 改成 `{tab === 'pending' && !pendingLoaded && <Skeleton variant="card-list" count={3} />}`

第 826 行（history tab）：`{tab === 'history' && !historyLoaded && <p className="text-ink-soft">加载中…</p>}` 改成 `{tab === 'history' && !historyLoaded && <Skeleton variant="card-list" count={3} />}`

- [ ] **Step 5: 列表卡片接入密度**

pending 卡片（第 629-632 行）从：

```tsx
          <div
            key={review.review_id}
            className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal"
          >
```

改成：

```tsx
          <div
            key={review.review_id}
            className={`flex flex-col gap-3 border-2 border-ink bg-card shadow-brutal ${
              density === 'compact' ? 'p-2.5' : 'p-4'
            }`}
          >
```

history 卡片（第 830-833 行）从：

```tsx
          <div
            key={review.review_id}
            className="flex flex-col gap-1 border-2 border-ink bg-card p-4 shadow-brutal-sm"
          >
```

改成：

```tsx
          <div
            key={review.review_id}
            className={`flex flex-col gap-1 border-2 border-ink bg-card shadow-brutal-sm ${
              density === 'compact' ? 'p-2.5' : 'p-4'
            }`}
          >
```

- [ ] **Step 6: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
git add frontend/src/admin/GraphReviewsPage.tsx
git commit -m "feat(admin): toast/empty-state/skeleton/density for GraphReviewsPage"
```

---

### Task 9: TermsPage.tsx —— toast、空状态真链接、骨架屏、密度

**Files:**
- Modify: `frontend/src/admin/TermsPage.tsx`

**Interfaces:**
- Consumes: `useToast()`（Task 1），`Skeleton`（Task 2），`useAdminDensity()`（Task 5），`Link` from `react-router-dom`

- [ ] **Step 1: 顶部 import**

第 1-7 行从：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminTenant } from './TenantContext'
import { deleteTerm, fetchTermsPage, updateTerm, type TermRecord } from './termsApi'
import { adminFetch } from './adminApi'
import { Pager } from './Pager'
```

改成：

```tsx
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { useConfirm } from './ConfirmContext'
import { useAdminDensity } from './DensityContext'
import { Skeleton } from './Skeleton'
import { useAdminTenant } from './TenantContext'
import { deleteTerm, fetchTermsPage, updateTerm, type TermRecord } from './termsApi'
import { useToast } from './ToastContext'
import { adminFetch } from './adminApi'
import { Pager } from './Pager'
```

组件函数体开头（`const confirm = useConfirm()` 附近）加：

```tsx
  const showToast = useToast()
  const { density } = useAdminDensity()
```

- [ ] **Step 2: 两处 toast**

`handleSaveEdit`（第 141-156 行）成功分支从：

```tsx
      await updateTerm(sessionToken, tenantId, originalStandardName, draftToRecord(editDraft))
      setEditingKey(null)
      setEditDraft(null)
      await refresh()
```

改成：

```tsx
      await updateTerm(sessionToken, tenantId, originalStandardName, draftToRecord(editDraft))
      showToast('已保存')
      setEditingKey(null)
      setEditDraft(null)
      await refresh()
```

`handleDelete`（第 158-171 行）成功分支从：

```tsx
      await deleteTerm(sessionToken, tenantId, standardName)
      await refresh()
```

改成：

```tsx
      await deleteTerm(sessionToken, tenantId, standardName)
      showToast('已删除实体')
      await refresh()
```

- [ ] **Step 3: 空状态改真链接**

第 317-321 行从：

```tsx
      {loaded && !error && terms.length === 0 && (
        <p className="text-ink-soft">
          还没有任何实体。实体创建只能通过「表格导入」或「文档抽取」完成。
        </p>
      )}
```

改成：

```tsx
      {loaded && !error && terms.length === 0 && (
        <p className="text-ink-soft">
          还没有任何实体。实体创建只能通过「
          <Link to="/admin/data-entry/etl" className="font-bold underline">
            表格导入
          </Link>
          」或「
          <Link to="/admin/data-entry/review" className="font-bold underline">
            文档抽取
          </Link>
          」完成。
        </p>
      )}
```

- [ ] **Step 4: 加载态换 Skeleton**

第 204 行 `{!loaded && <p className="text-ink-soft">加载中…</p>}` 改成 `{!loaded && <Skeleton variant="table-rows" count={5} />}`

- [ ] **Step 5: 列表接入密度**

第 209-211 行从：

```tsx
              key={term.standard_name}
              className="flex flex-col gap-3 border-2 border-ink bg-card p-4 shadow-brutal-sm"
            >
```

改成：

```tsx
              key={term.standard_name}
              className={`flex flex-col gap-3 border-2 border-ink bg-card shadow-brutal-sm ${
                density === 'compact' ? 'p-2.5' : 'p-4'
              }`}
            >
```

- [ ] **Step 6: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
git add frontend/src/admin/TermsPage.tsx
git commit -m "feat(admin): toast/empty-state-links/skeleton/density for TermsPage"
```

---

### Task 10: SchemaEtlPage.tsx —— toast、空状态同页提示

**Files:**
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`

**Interfaces:**
- Consumes: `useToast()`（Task 1）。本文件不接入 Skeleton（没有独立加载态代码）、不接入密度（不在密度改造范围内，见 spec 决策 5）。

- [ ] **Step 1: 顶部 import**

第 1-7 行从：

```tsx
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { SchemaEtlConfigBuilder } from './schemaEtlConfigBuilder/SchemaEtlConfigBuilder'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { CopyButton } from './CopyButton'
import { TaskStatusBadge } from './TaskStatusBadge'
```

改成：

```tsx
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { adminFetch, extractErrorDetail } from './adminApi'
import { SchemaEtlConfigBuilder } from './schemaEtlConfigBuilder/SchemaEtlConfigBuilder'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
import { CopyButton } from './CopyButton'
import { TaskStatusBadge } from './TaskStatusBadge'
```

组件函数体开头加一行 `const showToast = useToast()`。

- [ ] **Step 2: `handleUpload`（提交运行）加 toast**

第 210-243 行成功分支从：

```tsx
      form.reset()
      await pollNowRef.current()
```

改成：

```tsx
      showToast('已提交运行')
      form.reset()
      await pollNowRef.current()
```

- [ ] **Step 3: 空状态加同页提示**

第 462 行从：

```tsx
        {runs.length === 0 && <p className="text-ink-soft">还没有任何跑批记录。</p>}
```

改成：

```tsx
        {runs.length === 0 && (
          <p className="text-ink-soft">还没有任何跑批记录。在上方上传数据文件开始第一次运行。</p>
        )}
```

- [ ] **Step 4: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/SchemaEtlPage.tsx
git commit -m "feat(admin): toast + same-page empty-state hint for SchemaEtlPage"
```

---

### Task 11: ChatSidebar.tsx —— 修复残留 window.confirm、toast、tooltip、空状态提示

**Files:**
- Modify: `frontend/src/components/ChatSidebar.tsx`

**Interfaces:**
- Consumes: `useConfirm()` from `../admin/ConfirmContext`（Task 1 提升为站点级后，前台组件现在也能用了），`useToast()`（Task 1），`Tooltip`（Task 4）

- [ ] **Step 1: 顶部 import**

第 1-2 行从：

```tsx
import { useState } from 'react'
import type { SessionSummary } from '../lib/sessionsApi'
```

改成：

```tsx
import { useState } from 'react'
import { useConfirm } from '../admin/ConfirmContext'
import { Tooltip } from '../admin/Tooltip'
import { useToast } from '../admin/ToastContext'
import type { SessionSummary } from '../lib/sessionsApi'
```

- [ ] **Step 2: 修复残留的 `window.confirm()`，加 toast**

组件函数体开头加：

```tsx
  const confirm = useConfirm()
  const showToast = useToast()
```

`handleDelete`（第 46-57 行）从：

```tsx
  const handleDelete = async (session: SessionSummary) => {
    if (!window.confirm(`确定要删除会话「${session.title}」吗？此操作不可撤销。`)) return
    setDeletingId(session.session_id)
    setDeleteError(null)
    try {
      await onDeleteSession(session.session_id)
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : '删除会话失败')
    } finally {
      setDeletingId(null)
    }
  }
```

改成：

```tsx
  const handleDelete = async (session: SessionSummary) => {
    if (!(await confirm(`确定要删除会话「${session.title}」吗？此操作不可撤销。`))) return
    setDeletingId(session.session_id)
    setDeleteError(null)
    try {
      await onDeleteSession(session.session_id)
      showToast('已删除会话')
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : '删除会话失败')
    } finally {
      setDeletingId(null)
    }
  }
```

- [ ] **Step 3: 删除按钮加 Tooltip**

第 95-103 行从：

```tsx
                <button
                  type="button"
                  onClick={() => handleDelete(session)}
                  disabled={deletingId === session.session_id}
                  aria-label={`删除会话「${session.title}」`}
                  className={`flex min-h-[44px] w-10 flex-shrink-0 cursor-pointer items-center justify-center border-2 border-ink bg-paper text-ink shadow-brutal-sm transition hover:bg-status-error-hover active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  <TrashIcon />
                </button>
```

改成：

```tsx
                <Tooltip label="删除会话">
                  <button
                    type="button"
                    onClick={() => handleDelete(session)}
                    disabled={deletingId === session.session_id}
                    aria-label={`删除会话「${session.title}」`}
                    className={`flex min-h-[44px] w-10 flex-shrink-0 cursor-pointer items-center justify-center border-2 border-ink bg-paper text-ink shadow-brutal-sm transition hover:bg-status-error-hover active:translate-x-px active:translate-y-px active:shadow-none disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                  >
                    <TrashIcon />
                  </button>
                </Tooltip>
```

- [ ] **Step 4: 空状态加同页提示**

第 76 行从：

```tsx
          <p className="p-2 text-sm text-ink-soft">还没有历史会话</p>
```

改成：

```tsx
          <p className="p-2 text-sm text-ink-soft">还没有历史会话，点击上方「+ 新建会话」开始</p>
```

- [ ] **Step 5: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/ChatSidebar.tsx
git commit -m "fix(chat): replace window.confirm with useConfirm, add toast + tooltip"
```

---

## 手工验证（全部任务完成后，浏览器里逐项确认）

1. 刷新前台聊天页 `/`：删除一个会话，确认弹窗是应用内样式（不是浏览器原生弹窗），删除后顶部出现"已删除会话" toast，3 秒后自动消失；把所有会话删光，空状态文案提示"点击上方「+ 新建会话」开始"；把鼠标悬停在删除图标上，0.15s 后出现"删除会话"提示条。
2. 后台"文档管理"：清空搜索/切租户到一个没有文档的租户，空状态文案里的"数据加工"是可点击链接；刷新页面观察加载瞬间是否显示方块骨架屏而不是"加载中…"文字；上传一个文件，提交后出现"已提交上传" toast；删除一个文档，出现"已删除文档" toast。
3. 后台"本体管理"：切到实体类型 tab，草稿视图下清空所有草稿，空状态提示"点击下方「+ 新增实体类型」创建一个"；切到已确认视图，空状态只是纯描述、没有按钮提示；触发一次"迁移实体类型"，确认原来常驻不消失的文字提示消失了，改成顶部 toast 一闪而过；约束 tab 的空状态提示是"在下方表单里添加一个"（不是"上方"）。
4. 后台"数据加工 → 表格导入"：提交一次跑批，出现"已提交运行" toast；跑批列表为空时提示"在上方上传数据文件开始第一次运行"；找一个状态为"运行中"的跑批记录，确认徽章文字前有个在闪烁的小方块。
5. 侧边栏新增的"列表密度"下拉框：切到"紧凑"，确认本体管理的表格、文档列表行距明显变小；切回"标准"恢复；刷新页面确认选择保留（localStorage 生效）。
6. 分页器（任意一个有多页数据的列表）：鼠标悬停在 ‹/› 箭头上，0.15s 后出现"上一页"/"下一页"提示条。
7. `prefers-reduced-motion` 检查（浏、览器 DevTools 里模拟该媒体查询）：toast 的淡入淡出、骨架屏的脉冲、活跃徽章的闪烁点、tooltip 的淡入应全部变成无动画的瞬时切换。
