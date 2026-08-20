# 管理后台皮肤系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给管理后台加一套可切换的配色皮肤（默认/暗色/商务蓝），管理员可以在侧边栏选自己偏好的配色，选择存在浏览器本地，跨会话保留。

**Architecture:** 把 Tailwind 的 `paper`/`ink`/`ink.soft`/`card` 四个颜色 token（以及引用它们的 `boxShadow`）从写死的 hex 改成引用 CSS 自定义属性；全局样式表里用 `:root` 定义默认值、`:root[data-skin="..."]` 定义覆盖值；新建一个 React Context（`SkinContext`）管理当前选中的皮肤 id，存 `localStorage`，用一个 `useEffect` 把它同步成 `<html>` 元素的 `data-skin` 属性，从而让 CSS 侧的覆盖规则生效；再配一个下拉框组件（`SkinSwitcher`）挂在侧边栏。纯前端实现，不涉及任何后端改动。

**Tech Stack:** React + TypeScript + Tailwind CSS（原生 CSS 自定义属性，不引入任何新 npm 依赖）。

**Spec:** `docs/superpowers/specs/2026-08-20-admin-skin-system-design.md`

## Global Constraints

- 只改配色 + 阴影颜色，不动字体、不动布局、不动组件结构。
- `accent`（pink/yellow/cyan/green/orange）和 `status`（success/error）系列在三套皮肤下保持完全一致，不做任何改动。
- 三套皮肤的精确色值（后面任务里会原样用到，这里先列全，不要凭空改动）：

  | Token | 默认 | 暗色 | 商务蓝 |
  |---|---|---|---|
  | `--color-paper` | `#FFFAEF` | `#151517` | `#F4F6F8` |
  | `--color-ink` | `#141111` | `#f9fafb` | `#1B2430` |
  | `--color-ink-soft` | `#5C5750` | `#cfd3d6` | `#5A6B7B` |
  | `--color-card` | `#FFFFFF` | `#2C2C2E` | `#FFFFFF` |

- 持久化用 `localStorage`（key: `admin_skin`），不是 `TenantContext` 用的 `sessionStorage`——皮肤偏好要跨浏览器会话保留。
- `SkinProvider` 的包裹范围要跟现有 `TenantProvider` 一致（包住侧边栏和 `<Outlet />` 两者），不要只包一边。
- 不处理刷新瞬间的 FOUC（一闪而过的默认皮肤），不在 `index.html` 加内联脚本。
- 前端没有自动化测试框架（`frontend/package.json` 未接入 vitest/jest），每个任务的验证手段是 `cd frontend && npx tsc --noEmit`（`frontend/tsconfig.json` 已确认 `noUnusedLocals: true`、`noUnusedParameters: true`，任何未使用的 import/变量都会导致这个命令失败），加上能实际跑的构建产物检查（见 Task 1）和终态的人工走查描述（本 session 没有浏览器自动化工具，走查步骤写成"预期应该看到什么"，由人或后续有浏览器能力的执行者实际核对，不假装能自动验证视觉效果）。

---

### Task 1: 把颜色 token 改成 CSS 变量驱动

**Files:**
- Modify: `frontend/tailwind.config.ts`
- Modify: `frontend/src/styles/index.css`

**Interfaces:**
- Consumes：无（本任务是最底层的基础设施改动）。
- Produces：四个 CSS 自定义属性名，供 Task 2/3 及所有已有组件通过既有的 `bg-paper`/`text-ink`/`border-ink`/`bg-card`/`shadow-brutal` 等 Tailwind class 间接使用（这些 class 名字完全不变，只是背后引用的颜色来源变了）：`--color-paper`、`--color-ink`、`--color-ink-soft`、`--color-card`。

- [ ] **Step 1: 修改 `frontend/tailwind.config.ts`**

当前文件内容（完整）：

```typescript
import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#FFFAEF',
        ink: {
          DEFAULT: '#141111',
          soft: '#5C5750',
        },
        card: '#FFFFFF',
        accent: {
          pink: '#FE7DA8',
          yellow: '#FFD440',
          cyan: '#27CCF3',
          green: '#A9D877',
          orange: '#F8A16F',
        },
        status: {
          success: '#A9D877',
          error: '#DC2626',
        },
      },
      fontFamily: {
        sans: [
          '"Space Grotesk"',
          'system-ui',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: ['"Space Mono"', 'ui-monospace', '"SFMono-Regular"', 'monospace'],
      },
      boxShadow: {
        brutal: '2px 2px 0 0 #141111',
        'brutal-sm': '1px 1px 0 0 #141111',
      },
    },
  },
  plugins: [],
} satisfies Config
```

用 Edit 工具把 `colors` 和 `boxShadow` 两块替换成：

```typescript
      colors: {
        paper: 'var(--color-paper)',
        ink: {
          DEFAULT: 'var(--color-ink)',
          soft: 'var(--color-ink-soft)',
        },
        card: 'var(--color-card)',
        accent: {
          pink: '#FE7DA8',
          yellow: '#FFD440',
          cyan: '#27CCF3',
          green: '#A9D877',
          orange: '#F8A16F',
        },
        status: {
          success: '#A9D877',
          error: '#DC2626',
        },
      },
```

```typescript
      boxShadow: {
        brutal: '2px 2px 0 0 var(--color-ink)',
        'brutal-sm': '1px 1px 0 0 var(--color-ink)',
      },
```

`fontFamily` 那一块原样不动（本次不改字体）。

- [ ] **Step 2: 修改 `frontend/src/styles/index.css`**

当前文件内容（完整）：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

body {
  @apply bg-paper text-ink font-sans;
}
```

用 Edit 工具在 `@tailwind utilities;` 和 `body {` 之间插入：

```css
:root {
  --color-paper: #FFFAEF;
  --color-ink: #141111;
  --color-ink-soft: #5C5750;
  --color-card: #FFFFFF;
}

:root[data-skin='dark'] {
  --color-paper: #151517;
  --color-ink: #f9fafb;
  --color-ink-soft: #cfd3d6;
  --color-card: #2C2C2E;
}

:root[data-skin='business-blue'] {
  --color-paper: #F4F6F8;
  --color-ink: #1B2430;
  --color-ink-soft: #5A6B7B;
  --color-card: #FFFFFF;
}
```

修改后完整文件应该是：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --color-paper: #FFFAEF;
  --color-ink: #141111;
  --color-ink-soft: #5C5750;
  --color-card: #FFFFFF;
}

:root[data-skin='dark'] {
  --color-paper: #151517;
  --color-ink: #f9fafb;
  --color-ink-soft: #cfd3d6;
  --color-card: #2C2C2E;
}

:root[data-skin='business-blue'] {
  --color-paper: #F4F6F8;
  --color-ink: #1B2430;
  --color-ink-soft: #5A6B7B;
  --color-card: #FFFFFF;
}

body {
  @apply bg-paper text-ink font-sans;
}
```

- [ ] **Step 3: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）——`tailwind.config.ts` 是 `.ts` 文件，这一步也会检查它的类型正确性。

- [ ] **Step 4: 构建产物核查——确认颜色真的走了 CSS 变量，不是还在某处写死**

Run:
```bash
cd frontend && npm run build
```

构建完成后，产物 CSS 在 `dist/assets/*.css`（具体文件名带 hash，用通配符）。运行：

```bash
grep -o 'box-shadow:[^;]*' dist/assets/*.css | sort -u
```

Expected：每一行 `box-shadow:` 规则的值里都包含 `var(--color-ink)`，不应该再出现任何一行是字面量 `#141111`（如果 `boxShadow` 那处替换漏改或改错，这里会直接看到写死的十六进制值，而不是变量引用）。

再运行：

```bash
grep -c 'var(--color-ink)' dist/assets/*.css
grep -c '\-\-color-paper: #FFFAEF' dist/assets/*.css
```

Expected：两条命令的输出都应该是不为 0 的数字（第一条确认 Tailwind 生成的 utility class 里真的在引用这个变量；第二条确认 `:root` 里的默认值声明确实被打进了产物）。

这一步做完后可以删掉 `dist/` 目录（`rm -rf dist`），它只是用来验证构建产物的临时产出，不需要提交到仓库（`.gitignore` 应该已经忽略了 `dist/`，用 `git status` 确认一下没有把它加进暂存区）。

- [ ] **Step 5: Commit**

```bash
git add frontend/tailwind.config.ts frontend/src/styles/index.css
git commit -m "feat(admin): drive paper/ink/card colors and shadow color from CSS variables"
```

---

### Task 2: `SkinContext` + `SkinSwitcher`

**Files:**
- Create: `frontend/src/admin/SkinContext.tsx`
- Create: `frontend/src/admin/SkinSwitcher.tsx`

**Interfaces:**
- Consumes：Task 1 定义的 CSS 变量名（本任务不直接引用它们，只是通过设置 `data-skin` 属性让 Task 1 里定义的 `:root[data-skin="..."]` 规则生效）。
- Produces：
  - `SkinContext.tsx` 导出：`type SkinId = 'default' | 'dark' | 'business-blue'`、`function SkinProvider({ children }: { children: ReactNode })`、`function useAdminSkin(): { skin: SkinId; setSkin: (next: SkinId) => void }`——Task 3 会 import 这三者。
  - `SkinSwitcher.tsx` 导出：`function SkinSwitcher()`（无 props）——Task 3 会直接渲染 `<SkinSwitcher />`。

- [ ] **Step 1: 创建 `frontend/src/admin/SkinContext.tsx`**

参照对象：`frontend/src/admin/TenantContext.tsx` 的现有写法（Context + Provider + hook 的三段式结构、`throw new Error` 的越界使用提示风格）。跟它的关键区别：用 `localStorage` 不是 `sessionStorage`；多一个 `useEffect` 把当前值同步到 DOM 属性上（`TenantContext` 没有这个副作用，因为它管理的状态不需要反映到 DOM 上）。

```typescript
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

const SKIN_STORAGE_KEY = 'admin_skin'

export type SkinId = 'default' | 'dark' | 'business-blue'

const VALID_SKIN_IDS: readonly SkinId[] = ['default', 'dark', 'business-blue']

function isSkinId(value: string | null): value is SkinId {
  return value !== null && (VALID_SKIN_IDS as readonly string[]).includes(value)
}

interface SkinContextValue {
  skin: SkinId
  setSkin: (next: SkinId) => void
}

const SkinContext = createContext<SkinContextValue | null>(null)

/**
 * 当前管理员选择的配色皮肤——个人偏好，存 localStorage（不是
 * TenantContext 用的 sessionStorage：皮肤偏好要跨浏览器会话保留，不像
 * "当前操作哪个租户"那样是会话级状态）。用 Context 是为了跟
 * TenantContext 保持同一套架构模式，即便目前只有 SkinSwitcher 自己读
 * 这个状态，也方便以后有别的地方需要读。
 */
export function SkinProvider({ children }: { children: ReactNode }) {
  const [skin, setSkinState] = useState<SkinId>(() => {
    const stored = localStorage.getItem(SKIN_STORAGE_KEY)
    return isSkinId(stored) ? stored : 'default'
  })

  // 把当前皮肤同步到 <html data-skin="..."> 上——index.css 里的
  // :root[data-skin="dark"] / :root[data-skin="business-blue"] 覆盖块
  // 靠这个属性生效，不设置属性时 :root 的默认值（即"默认"皮肤）生效。
  useEffect(() => {
    document.documentElement.setAttribute('data-skin', skin)
  }, [skin])

  const value = useMemo<SkinContextValue>(
    () => ({
      skin,
      setSkin: (next: SkinId) => {
        localStorage.setItem(SKIN_STORAGE_KEY, next)
        setSkinState(next)
      },
    }),
    [skin],
  )

  return <SkinContext.Provider value={value}>{children}</SkinContext.Provider>
}

export function useAdminSkin(): SkinContextValue {
  const value = useContext(SkinContext)
  if (value === null) {
    throw new Error('useAdminSkin() 必须在 <SkinProvider> 内部使用')
  }
  return value
}
```

- [ ] **Step 2: 创建 `frontend/src/admin/SkinSwitcher.tsx`**

参照对象：`frontend/src/admin/TenantSwitcher.tsx` 的 `<label>` + `<select>` 部分（不需要它"新建租户"表单那部分——皮肤是固定列表，不能新建）。注意 `TenantSwitcher.tsx` 的 `<select>` 本身没有用 `focusRing`（只有按钮用了），本文件同理不需要声明 `focusRing` 常量，声明了也不用会被 `noUnusedLocals` 挡下来。

```typescript
import { useAdminSkin, type SkinId } from './SkinContext'

const SKIN_OPTIONS: { id: SkinId; label: string }[] = [
  { id: 'default', label: '默认' },
  { id: 'dark', label: '暗色' },
  { id: 'business-blue', label: '商务蓝' },
]

export function SkinSwitcher() {
  const { skin, setSkin } = useAdminSkin()

  return (
    <label className="flex flex-col gap-1 text-xs font-bold uppercase tracking-wide text-ink-soft">
      配色皮肤
      <select
        value={skin}
        onChange={(event) => setSkin(event.target.value as SkinId)}
        aria-label="切换配色皮肤"
        className="min-h-[44px] w-full border-2 border-ink bg-paper px-2 text-sm font-bold text-ink"
      >
        {SKIN_OPTIONS.map((option) => (
          <option key={option.id} value={option.id}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  )
}
```

- [ ] **Step 3: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）。

- [ ] **Step 4: 手工走查**

确认 `SkinProvider` 的初始化读取逻辑：`localStorage` 里没有 `admin_skin` 这个 key（全新浏览器/清过缓存）时，`isSkinId(null)` 返回 `false`，`useState` 的初始值回退到 `'default'`，不会抛异常、不会渲染出一个不存在于 `SKIN_OPTIONS` 里的选项。确认 `useEffect` 的依赖数组是 `[skin]`——只依赖这一个状态，不会重演之前遇到过的"依赖数组包含自己写的状态导致自我取消"那类 bug（这里没有异步请求、没有 `cancelled` 标志，纯同步的 DOM 属性赋值，不存在那类风险，但仍然确认一下依赖数组本身没有多余或缺失的项）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/admin/SkinContext.tsx frontend/src/admin/SkinSwitcher.tsx
git commit -m "feat(admin): add SkinContext and SkinSwitcher for the admin color-skin picker"
```

---

### Task 3: 接入 `AdminLayout.tsx`

**Files:**
- Modify: `frontend/src/admin/AdminLayout.tsx`

**Interfaces:**
- Consumes：Task 2 的 `SkinProvider`（组件）、`SkinSwitcher`（组件）。
- Produces：无（叶子集成点，不被其他文件消费）。

- [ ] **Step 1: 修改 `frontend/src/admin/AdminLayout.tsx`**

当前文件内容（完整）：

```typescript
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `border-2 border-ink px-3 py-2.5 text-sm font-bold transition ${focusRing} ${
    isActive ? 'bg-accent-pink text-ink shadow-brutal-sm' : 'bg-paper text-ink hover:bg-card'
  }`

export function AdminLayout() {
  const { sessionToken, logout } = useAdminAuth()

  if (!sessionToken) {
    return <Navigate to="/admin/login" replace />
  }

  // TenantProvider 必须包住侧边栏（租户下拉框）和 <Outlet />（各页面）两者，
  // 它们才共用同一份租户状态；只包其中一边等于没修。
  //
  // 侧边栏在窄屏（<768px）下改成顶部横条：flex-col 让 aside 和 main 上下堆叠、
  // aside 内部改 flex-row 排布，避免固定 w-56 的侧边栏在手机宽度下把主内容区
  // 挤到不到 150px 宽、必须横向滚动才能看全的问题。
  return (
    <TenantProvider>
      <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
        <aside className="flex flex-col gap-3 border-b-2 border-ink bg-card p-4 md:w-56 md:flex-shrink-0 md:flex-col md:justify-between md:border-b-0 md:border-r-2">
          <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
            <NavLink to="/admin/ontology" className={navLinkClass}>
              本体管理
            </NavLink>
            <NavLink to="/admin/documents" className={navLinkClass}>
              文档管理
            </NavLink>
            <NavLink to="/admin/data-entry" className={navLinkClass}>
              数据加工
            </NavLink>
          </nav>
          <div className="flex flex-row flex-wrap gap-3 md:flex-col">
            <TenantSwitcher />
            <Link
              to="/"
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-center text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              返回前台
            </Link>
            <button
              type="button"
              onClick={logout}
              className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
            >
              登出
            </button>
          </div>
        </aside>
        <main className="flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </TenantProvider>
  )
}
```

用 Edit 工具把导入区替换成（新增两行 import）：

```typescript
import { Link, NavLink, Navigate, Outlet } from 'react-router-dom'
import { useAdminAuth } from './useAdminAuth'
import { SkinProvider } from './SkinContext'
import { SkinSwitcher } from './SkinSwitcher'
import { TenantProvider } from './TenantContext'
import { TenantSwitcher } from './TenantSwitcher'
```

把 `return (` 到函数结尾的 `)` 那一整块替换成（`SkinProvider` 包在 `TenantProvider` 外层，包裹范围跟 `TenantProvider` 一致；`<SkinSwitcher />` 紧跟在 `<TenantSwitcher />` 后面）：

```typescript
  return (
    <SkinProvider>
      <TenantProvider>
        <div className="flex min-h-dvh flex-col bg-paper md:flex-row">
          <aside className="flex flex-col gap-3 border-b-2 border-ink bg-card p-4 md:w-56 md:flex-shrink-0 md:flex-col md:justify-between md:border-b-0 md:border-r-2">
            <nav className="flex flex-row flex-wrap gap-2 md:flex-col">
              <NavLink to="/admin/ontology" className={navLinkClass}>
                本体管理
              </NavLink>
              <NavLink to="/admin/documents" className={navLinkClass}>
                文档管理
              </NavLink>
              <NavLink to="/admin/data-entry" className={navLinkClass}>
                数据加工
              </NavLink>
            </nav>
            <div className="flex flex-row flex-wrap gap-3 md:flex-col">
              <TenantSwitcher />
              <SkinSwitcher />
              <Link
                to="/"
                className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-center text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
              >
                返回前台
              </Link>
              <button
                type="button"
                onClick={logout}
                className={`min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-2 text-sm font-bold text-ink shadow-brutal-sm transition active:translate-x-px active:translate-y-px active:shadow-none ${focusRing}`}
              >
                登出
              </button>
            </div>
          </aside>
          <main className="flex-1 overflow-y-auto p-6">
            <Outlet />
          </main>
        </div>
      </TenantProvider>
    </SkinProvider>
  )
}
```

（函数体里 `TenantProvider 必须包住...` 那段注释原样保留在 `return (` 之前不动，本次不改动那段说明——它描述的是 `TenantProvider` 自己的包裹要求，仍然成立。）

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出（干净通过）。

- [ ] **Step 3: 人工走查（预期结果描述——本 session 没有浏览器自动化工具，这一步由能实际打开浏览器的人或后续会话核对，不假装能自动跑通）**

打开 `http://localhost:5173/admin/documents`（需要先登录管理后台），预期看到：

1. 侧边栏底部、"切换租户"下拉框下方、"返回前台"按钮上方，新增一个"配色皮肤"下拉框，默认选中"默认"。
2. 切换到"暗色"：整个页面背景从暖白色（`#FFFAEF`）变成接近黑色的深色（`#151517`）；文字、边框颜色从深黑色（`#141111`）变成米白色（`#f9fafb`）；卡片类背景（比如侧边栏 `<aside>` 的背景）变成深灰色（`#2C2C2E`）；所有原本用粗黑色阴影的按钮/卡片，阴影颜色也应该跟着变成米白色（因为 `box-shadow` 现在引用同一个 `--color-ink` 变量）。
3. 切换到"商务蓝"：背景变冷灰蓝（`#F4F6F8`），文字/边框变深藏青（`#1B2430`）。
4. 三套皮肤下，强调色（比如"上传文档"按钮的粉色背景、警告用的黄色背景）应该完全不变——这是 Global Constraints 里"accent/status 不随皮肤变化"的直接验证点。
5. 切换皮肤后刷新整个页面（F5），之前选的皮肤应该保持（从 `localStorage` 读回来），不会跳回"默认"。
6. 用浏览器开发者工具查看 `<html>` 元素，应该能看到 `data-skin="dark"`（或对应选中的皮肤 id）这个属性随着下拉框切换实时更新。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/admin/AdminLayout.tsx
git commit -m "feat(admin): wire SkinProvider and SkinSwitcher into AdminLayout"
```

---

## 完成后的整体验证

1. `cd frontend && npx tsc --noEmit` 全程干净。
2. Task 1 的构建产物 grep 检查（`var(--color-ink)` 确实出现在编译后的 CSS 里，没有遗漏的写死颜色）。
3. Task 3 的六点人工走查（无浏览器自动化工具的已知限制下，由人或后续会话实际核对）。
