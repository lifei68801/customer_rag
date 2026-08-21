# 视觉语言转向（圆角+柔和阴影+细边框）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把整个项目（前台聊天页 + 后台管理）从新粗野主义（0 圆角、2px 实边黑框、硬直角偏移阴影）转向 DSH 风格（圆角、1px 半透明细边、柔和多层阴影），点击反馈从"位移+阴影消失"改成"缩放+透明度"。

**Architecture:** 先改 `tailwind.config.ts`/`index.css` 里的 token 定义（阴影值、新增圆角尺度、新增边框/阴影颜色变量）——这一步是全局生效的，不用碰任何组件文件。再做 3 轮全局机械替换（shadow 类改名、border 宽度+颜色改名、点击反馈类名替换）——用 sed 一次性扫过整个 `frontend/src`，因为都是"同一种模式全文件替换"。最后按目录分 3 批，给每个文件里已经有边框/阴影的元素逐一补上语义化圆角 class（这一步没法用 sed，因为是"新增"不是"替换"，需要判断每个元素的角色对应哪个圆角档位）。

**Tech Stack:** React + TypeScript + Tailwind。项目无自动化前端测试框架，验证手段是 `npx tsc --noEmit` + 手工浏览器验证。

**Spec:** `docs/superpowers/specs/2026-08-21-shape-language-shift.md`

## Global Constraints

- 圆角档位（`tailwind.config.ts` 的 `borderRadius`）：`chip: 6px`（徽章/标签）、`control: 7px`（按钮/输入框）、`DEFAULT: 8px`（兜底）、`card: 12px`（列表卡片/表格外框）、`panel: 14px`（表单区块/toast）、`modal: 18px`（弹窗/tooltip）、`container: 24px`（页面级大容器）、`full: 9999px`（圆形，Tailwind 默认值不用改）。
- 阴影：`shadow-brutal`/`shadow-brutal-sm` 改名成 `shadow-soft`/`shadow-soft-sm`，值改成引用新增的 `--shadow-color-1`（外层淡）/`--shadow-color-2`（内层深）两个 CSS 变量的多层柔和阴影。
- 边框：`border-2 border-ink` → `border border-subtle`（`border-subtle` 是新增语义色 token，指向 `--color-border-subtle`）。`border-2 border-status-error` → `border border-status-error`（宽度降级但颜色保持满饱和度不淡化，因为是错误强调）。
- 点击反馈：`active:translate-x-px active:translate-y-px active:shadow-none`（含 `active:translate-x-[2px] active:translate-y-[2px]` 变体）→ `active:scale-95 active:opacity-90`。Tailwind 的 `transition` 工具类默认已包含 `transform`/`opacity`，元素只要已有 `transition` class 就不需要额外加 `transition-transform`。
- 新增 CSS 变量（`frontend/src/styles/index.css`，三套皮肤 `:root`/`:root[data-skin='dark']`/`:root[data-skin='business-blue']` 各一份，具体数值见 Task 1）：`--color-border-subtle`、`--shadow-color-1`、`--shadow-color-2`。
- `border-subtle` 不能写成 `border-ink/10` 这种 Tailwind 透明度修饰符形式——本项目的自定义颜色 token（值是 `var(--color-x)`）不支持 `/opacity` 修饰符语法（编译不出对应 class，本会话已踩过这个坑），`--color-border-subtle` 必须是独立的、直接带 `rgba(...)` alpha 通道的 CSS 变量。
- 项目校验方式：每个任务完成后运行 `cd frontend && npx tsc --noEmit`，必须无输出。全部任务都改了 `tailwind.config.ts`（Task 1），所以 Task 1 完成后必须完整重启前端（杀 Vite 进程 + 清 `node_modules/.vite` 缓存 + 重启），后续任务（2 号之后）都是纯 `.tsx`/`.css` 内容编辑，不再碰 `tailwind.config.ts`，可以依赖热更新。
- 三套皮肤的圆角值/阴影层数结构/边框宽度完全一致，只有 Task 1 里列出的颜色深浅数值按皮肤背景做了适配，不算"形状语言不统一"。

---

### Task 1: Token 基础设施 —— tailwind.config.ts + index.css

**Files:**
- Modify: `frontend/tailwind.config.ts`
- Modify: `frontend/src/styles/index.css`

**Interfaces:**
- Produces：新 Tailwind class `border-subtle`（颜色）、`shadow-soft`/`shadow-soft-sm`（阴影，替代原 `shadow-brutal`/`shadow-brutal-sm`）、`rounded-chip`/`rounded-control`/`rounded-card`/`rounded-panel`/`rounded-modal`/`rounded-container`（圆角，Task 5-7 消费）

- [ ] **Step 1: 修改 `frontend/tailwind.config.ts`**

当前内容（已知，供比对）：

```ts
import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: 'var(--color-paper)',
        ink: {
          DEFAULT: 'var(--color-ink)',
          soft: 'var(--color-ink-soft)',
        },
        card: 'var(--color-card)',
        interactive: {
          hover: 'var(--color-interactive-hover)',
        },
        accent: {
          pink: 'var(--color-accent-pink)',
          yellow: 'var(--color-accent-yellow)',
          cyan: 'var(--color-accent-cyan)',
          green: 'var(--color-accent-green)',
          orange: 'var(--color-accent-orange)',
        },
        status: {
          success: 'var(--color-status-success)',
          error: 'var(--color-status-error)',
          'error-hover': 'var(--color-status-error-hover)',
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
        brutal: '2px 2px 0 0 var(--color-ink)',
        'brutal-sm': '1px 1px 0 0 var(--color-ink)',
      },
    },
  },
  plugins: [],
} satisfies Config
```

替换成：

```ts
import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: 'var(--color-paper)',
        ink: {
          DEFAULT: 'var(--color-ink)',
          soft: 'var(--color-ink-soft)',
        },
        card: 'var(--color-card)',
        interactive: {
          hover: 'var(--color-interactive-hover)',
        },
        accent: {
          pink: 'var(--color-accent-pink)',
          yellow: 'var(--color-accent-yellow)',
          cyan: 'var(--color-accent-cyan)',
          green: 'var(--color-accent-green)',
          orange: 'var(--color-accent-orange)',
        },
        status: {
          success: 'var(--color-status-success)',
          error: 'var(--color-status-error)',
          'error-hover': 'var(--color-status-error-hover)',
        },
        border: {
          subtle: 'var(--color-border-subtle)',
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
      borderRadius: {
        chip: '6px',
        control: '7px',
        DEFAULT: '8px',
        card: '12px',
        panel: '14px',
        modal: '18px',
        container: '24px',
      },
      boxShadow: {
        soft: '0 4px 6px -1px var(--shadow-color-1), 0 2px 4px -2px var(--shadow-color-2)',
        'soft-sm': '0 1px 3px 0 var(--shadow-color-1), 0 1px 2px -1px var(--shadow-color-2)',
      },
    },
  },
  plugins: [],
} satisfies Config
```

（`colors.border.subtle` 让 Tailwind 生成 `border-subtle` 这个颜色 class——跟 `colors.status.error` 生成 `border-status-error`/`bg-status-error`/`text-status-error` 是同一个机制，Tailwind 会自动把这个颜色值套到 `border-*`/`bg-*`/`text-*`/`ring-*` 等所有颜色系前缀上，这里只会用到 `border-subtle`。）

- [ ] **Step 2: 修改 `frontend/src/styles/index.css`——三个 `:root` 块各加 3 行**

当前内容（已知，供比对）：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --color-paper: #FFFAEF;
  --color-ink: #141111;
  --color-ink-soft: #5C5750;
  --color-card: #FFFFFF;
  --color-interactive-hover: #FFFFFF;
  --color-accent-pink: #FE7DA8;
  --color-accent-yellow: #FFD440;
  --color-accent-cyan: #27CCF3;
  --color-accent-green: #A9D877;
  --color-accent-orange: #F8A16F;
  --color-status-success: #A9D877;
  --color-status-error: #DC2626;
  --color-status-error-hover: #FBD8D8;
}

:root[data-skin='dark'] {
  --color-paper: #151517;
  --color-ink: #f9fafb;
  --color-ink-soft: #cfd3d6;
  --color-card: #2C2C2E;
  --color-interactive-hover: #2C2C2E;
  --color-accent-pink: #FF7AB8;
  --color-accent-yellow: #FFD93D;
  --color-accent-cyan: #38D9F0;
  --color-accent-green: #95E066;
  --color-accent-orange: #FFA45C;
  --color-status-success: #95E066;
  --color-status-error: #FF5C5C;
  --color-status-error-hover: #3A2226;
}

:root[data-skin='business-blue'] {
  --color-paper: #F4F6F8;
  --color-ink: #1B2430;
  --color-ink-soft: #5A6B7B;
  --color-card: #FFFFFF;
  --color-interactive-hover: #FFFFFF;
  --color-accent-pink: #6E7FB0;
  --color-accent-yellow: #5D988E;
  --color-accent-cyan: #4A87A6;
  --color-accent-green: #6B9B7C;
  --color-accent-orange: #A67C52;
  --color-status-success: #6B9B7C;
  --color-status-error: #B33A3A;
  --color-status-error-hover: #EAD9D9;
}

body {
  @apply bg-paper text-ink font-sans;
}
```

替换成：

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --color-paper: #FFFAEF;
  --color-ink: #141111;
  --color-ink-soft: #5C5750;
  --color-card: #FFFFFF;
  --color-interactive-hover: #FFFFFF;
  --color-accent-pink: #FE7DA8;
  --color-accent-yellow: #FFD440;
  --color-accent-cyan: #27CCF3;
  --color-accent-green: #A9D877;
  --color-accent-orange: #F8A16F;
  --color-status-success: #A9D877;
  --color-status-error: #DC2626;
  --color-status-error-hover: #FBD8D8;
  --color-border-subtle: rgba(20, 17, 17, 0.12);
  --shadow-color-1: rgba(20, 17, 17, 0.08);
  --shadow-color-2: rgba(20, 17, 17, 0.14);
}

:root[data-skin='dark'] {
  --color-paper: #151517;
  --color-ink: #f9fafb;
  --color-ink-soft: #cfd3d6;
  --color-card: #2C2C2E;
  --color-interactive-hover: #2C2C2E;
  --color-accent-pink: #FF7AB8;
  --color-accent-yellow: #FFD93D;
  --color-accent-cyan: #38D9F0;
  --color-accent-green: #95E066;
  --color-accent-orange: #FFA45C;
  --color-status-success: #95E066;
  --color-status-error: #FF5C5C;
  --color-status-error-hover: #3A2226;
  --color-border-subtle: rgba(249, 250, 251, 0.14);
  --shadow-color-1: rgba(0, 0, 0, 0.35);
  --shadow-color-2: rgba(0, 0, 0, 0.5);
}

:root[data-skin='business-blue'] {
  --color-paper: #F4F6F8;
  --color-ink: #1B2430;
  --color-ink-soft: #5A6B7B;
  --color-card: #FFFFFF;
  --color-interactive-hover: #FFFFFF;
  --color-accent-pink: #6E7FB0;
  --color-accent-yellow: #5D988E;
  --color-accent-cyan: #4A87A6;
  --color-accent-green: #6B9B7C;
  --color-accent-orange: #A67C52;
  --color-status-success: #6B9B7C;
  --color-status-error: #B33A3A;
  --color-status-error-hover: #EAD9D9;
  --color-border-subtle: rgba(27, 36, 48, 0.12);
  --shadow-color-1: rgba(27, 36, 48, 0.08);
  --shadow-color-2: rgba(27, 36, 48, 0.14);
}

body {
  @apply bg-paper text-ink font-sans;
}
```

- [ ] **Step 3: 完整重启前端 dev server（改了 `tailwind.config.ts`，热更新不可靠）**

Windows 环境下杀掉监听 5173 端口的进程和它的 esbuild 子进程，清 `frontend/node_modules/.vite` 缓存，再重新 `npm run dev`。（如果你的执行环境不是 Windows/这套特定工具链，用你环境里等价的"完全重启 dev server + 清构建缓存"操作即可，目的是确保 Tailwind 真正重新读取了新的 `tailwind.config.ts`。）

- [ ] **Step 4: 验证**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

用 curl 或类似方式确认编译后的 CSS 里能看到新 token 生效，例如请求 `http://localhost:5173/src/styles/index.css`（dev server 跑起来之后），确认里面出现 `--color-border-subtle`、`--shadow-color-1`、`.shadow-soft` 相关的规则、以及 `.rounded-card { border-radius: 12px }` 这类新圆角规则（此时还没有任何组件用到这些新类，但 Tailwind 只会生成"内容里出现过的" class——如果一个新 class 从没在任何 `.tsx` 里被引用过，Tailwind 不会生成它。这一步只是确认 token 定义本身没写错；真正验证 `rounded-card` 等圆角类生成，要等 Task 5-7 组件里用上之后再看）。

- [ ] **Step 5: Commit**

```bash
git add frontend/tailwind.config.ts frontend/src/styles/index.css
git commit -m "feat(design): add DSH-inspired border-radius/shadow/border tokens"
```

---

### Task 2: 全局机械替换 A —— 阴影类改名

**Files:**
- Modify: 全部使用 `shadow-brutal`/`shadow-brutal-sm` 的 `.tsx` 文件（`frontend/src` 下，Task 1 完成后运行 `grep -rl "shadow-brutal" frontend/src --include="*.tsx"` 可以拿到完整文件列表，预计 40+ 个文件）

**Interfaces:**
- Consumes：Task 1 产出的 `shadow-soft`/`shadow-soft-sm` class

- [ ] **Step 1: 运行替换（注意顺序：先替换更长的 `shadow-brutal-sm`，避免它被 `shadow-brutal` 的替换规则提前吃掉一部分）**

```bash
cd frontend/src
grep -rl "shadow-brutal-sm" --include="*.tsx" . | xargs sed -i 's/shadow-brutal-sm/shadow-soft-sm/g'
grep -rl "shadow-brutal\b" --include="*.tsx" . | xargs sed -i 's/shadow-brutal\b/shadow-soft/g'
```

（如果你的 shell 环境 `sed -i` 语法不同——比如 macOS BSD sed 需要 `sed -i ''`——用你环境里正确的 in-place 编辑语法，效果等价即可。）

- [ ] **Step 2: 验证替换完整且没有误伤**

```bash
grep -rn "shadow-brutal" --include="*.tsx" frontend/src
```

Expected: 无输出（一个 `shadow-brutal`/`shadow-brutal-sm` 都不剩）。

```bash
grep -rn "shadow-soft" --include="*.tsx" frontend/src | wc -l
```

Expected: 输出的行数应该等于替换前 `shadow-brutal`+`shadow-brutal-sm` 的总引用数（219 左右，具体以替换前你自己 grep 到的数字为准，不要凭空信这个数字，自己替换前后各 grep 一次比对）。

- [ ] **Step 3: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(design): rename shadow-brutal(-sm) to shadow-soft(-sm)"
```

---

### Task 3: 全局机械替换 B —— 边框宽度+颜色

**Files:**
- Modify: 全部使用 `border-2 border-ink`/`border-2 border-status-error` 的 `.tsx` 文件

**Interfaces:**
- Consumes：Task 1 产出的 `border-subtle` 颜色 class

- [ ] **Step 1: 运行替换**

```bash
cd frontend/src
grep -rl "border-2 border-ink\b" --include="*.tsx" . | xargs sed -i 's/border-2 border-ink\b/border border-subtle/g'
grep -rl "border-2 border-status-error" --include="*.tsx" . | xargs sed -i 's/border-2 border-status-error/border border-status-error/g'
```

注意 `border-ink` 后面那个 `\b`（单词边界）——防止误伤 `border-ink-soft`（这是完全不同的一个 class，本来就该保持 `border-ink-soft` 不动，不在这次替换范围内）。执行完之后专门确认一下 `border-ink-soft` 出现的地方没被动过：

```bash
grep -n "border-ink-soft" --include="*.tsx" -r .
```

Expected：这些行的 `border-ink-soft` 前面不应该出现被误改的痕迹（比如变成 `border border-subtle-soft` 这种荒谬结果——如果出现说明 `\b` 边界没生效，需要人工检查修正）。

- [ ] **Step 2: 验证替换完整**

```bash
grep -rn "border-2 border-ink\b\|border-2 border-status-error" --include="*.tsx" .
```

Expected: 无输出。

- [ ] **Step 3: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(design): border-2 solid -> border 1px subtle/status-colored"
```

---

### Task 4: 全局机械替换 C —— 点击反馈从位移改缩放

**Files:**
- Modify: 全部使用 `active:translate-x-px`/`active:translate-y-px`/`active:translate-x-[2px]`/`active:translate-y-[2px]`/`active:shadow-none` 组合的 `.tsx` 文件

**Interfaces:**
- 无新 token 依赖，纯 Tailwind 内置 `scale`/`opacity` 工具类

- [ ] **Step 1: 运行替换（这 3 个 class 总是成组出现在同一个模板字符串里，用 sed 把整个三件套替换成两件套）**

```bash
cd frontend/src
grep -rl "active:translate-x-px active:translate-y-px active:shadow-none" --include="*.tsx" . \
  | xargs sed -i 's/active:translate-x-px active:translate-y-px active:shadow-none/active:scale-95 active:opacity-90/g'
grep -rl "active:translate-x-\[2px\] active:translate-y-\[2px\] active:shadow-none" --include="*.tsx" . \
  | xargs sed -i 's/active:translate-x-\[2px\] active:translate-y-\[2px\] active:shadow-none/active:scale-95 active:opacity-90/g'
```

- [ ] **Step 2: 检查有没有漏网的变体写法**

```bash
grep -rn "active:translate-\|active:shadow-none" --include="*.tsx" .
```

Expected：理想情况下无输出。如果还有残留（比如三个 class 顺序不同、或者中间被别的 class 隔开、或者还有本计划没预见到的第三种位移数值），把每一处找到的都按同样的语义（去掉位移+阴影消失，改成 `active:scale-95 active:opacity-90`）手动改掉，不要留下任何 `active:translate-*`/`active:shadow-none`。

- [ ] **Step 3: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(design): active-press feedback from translate+shadow-none to scale+opacity"
```

---

### Task 5: 圆角落地 —— 后台管理页面（第一批：数据/本体管理主页面）

**Files:**
- Modify: `frontend/src/admin/OntologySchemaPage.tsx`
- Modify: `frontend/src/admin/DocumentsPage.tsx`
- Modify: `frontend/src/admin/GraphReviewsPage.tsx`
- Modify: `frontend/src/admin/TermsPage.tsx`
- Modify: `frontend/src/admin/SchemaEtlPage.tsx`

**Interfaces:**
- Consumes：Task 1 产出的 `rounded-chip`/`rounded-control`/`rounded-card`/`rounded-panel`/`rounded-modal` class；Task 2-4 已经把这些文件里的阴影/边框/点击反馈换过一轮，本任务只新增圆角，不再碰阴影/边框/点击反馈相关的 class

**方法论**（这一步没法用 sed，因为是"新增" class 不是"替换"，需要判断每个元素的角色）：

1. 在每个文件里搜索 `border-subtle`、`border-status-error`（Task 3 的产物）和 `shadow-soft`/`shadow-soft-sm`（Task 2 的产物）——这些 class 出现的地方，就是"这个元素原本有边框/阴影"的标记，也就是需要判断要不要补圆角的候选元素。
2. 按 spec（`docs/superpowers/specs/2026-08-21-shape-language-shift.md`）"圆角档位 → 组件角色映射"表格判断每个候选元素的角色，加上对应的 `rounded-*` class：
   - `<button>`（含 disabled 态的按钮）→ `rounded-control`
   - `<input>`/`<select>`/`<textarea>` → `rounded-control`
   - 徽章/小标签（比如 `TaskStatusBadge` 渲染出来的 `<span>`、"来源：xxx"这类小标签）→ `rounded-chip`
   - 列表卡片、表格外层 `overflow-x-auto` 容器 → `rounded-card`
   - 新增/编辑表单的外层区块（`shadow-soft` 那层容器）→ `rounded-panel`
3. 每加一个 `rounded-*`，插入位置跟着这个元素其它形状相关的 class 放在一起（比如 `border border-subtle rounded-card bg-card shadow-soft-sm` 这种顺序，圆角紧跟边框），不要打乱这个 class 字符串里其它无关部分的顺序。

**举例**（`OntologySchemaPage.tsx` 你会读到类似这样的按钮，Task 3/4 跑完之后应该长这样）：

```tsx
className={`min-h-[44px] cursor-pointer border border-subtle bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
```

加上圆角后：

```tsx
className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-pink px-4 py-2 text-sm font-bold text-ink shadow-soft-sm transition active:scale-95 active:opacity-90 ${focusRing}`}
```

再举一个表格外框的例子（`overflow-x-auto border border-subtle bg-card shadow-soft-sm` 这种容器）：

```tsx
<div className="overflow-x-auto rounded-card border border-subtle bg-card shadow-soft-sm">
```

- [ ] **Step 1: `OntologySchemaPage.tsx`** —— 按上述方法论逐一处理：主组件的确认按钮、`TermTypesTab`/`RelationTypesTab`/`ConstraintsTab` 三个 tab 各自的按钮、表格外框、新增/编辑表单区块

- [ ] **Step 2: `DocumentsPage.tsx`** —— 上传表单区块、文档列表卡片、按钮

- [ ] **Step 3: `GraphReviewsPage.tsx`** —— pending/history 两种列表卡片、批准/驳回按钮、批量操作区块

- [ ] **Step 4: `TermsPage.tsx`** —— 实体列表行、编辑表单里的输入框

- [ ] **Step 5: `SchemaEtlPage.tsx`** —— 上传表单区块、历史跑批表格

- [ ] **Step 6: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
git add frontend/src/admin/OntologySchemaPage.tsx frontend/src/admin/DocumentsPage.tsx frontend/src/admin/GraphReviewsPage.tsx frontend/src/admin/TermsPage.tsx frontend/src/admin/SchemaEtlPage.tsx
git commit -m "feat(design): apply border-radius tokens to admin data pages"
```

---

### Task 6: 圆角落地 —— 后台管理共享组件 + 剩余后台页面

**Files:**
- Modify: `frontend/src/admin/AdminLayout.tsx`
- Modify: `frontend/src/admin/LoginPage.tsx`
- Modify: `frontend/src/admin/DataEntryPage.tsx`
- Modify: `frontend/src/admin/SkinSwitcher.tsx`
- Modify: `frontend/src/admin/DensitySwitcher.tsx`
- Modify: `frontend/src/admin/TenantSwitcher.tsx`
- Modify: `frontend/src/admin/Pager.tsx`
- Modify: `frontend/src/admin/TaskStatusBadge.tsx`
- Modify: `frontend/src/admin/CopyButton.tsx`
- Modify: `frontend/src/admin/ToastContext.tsx`
- Modify: `frontend/src/admin/ConfirmContext.tsx`
- Modify: `frontend/src/admin/Tooltip.tsx`
- Modify: `frontend/src/admin/Skeleton.tsx`
- Modify: `frontend/src/admin/StandardNameInput.tsx`
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/SchemaEtlConfigBuilder.tsx`
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/EntityMappingEditor.tsx`
- Modify: `frontend/src/admin/schemaEtlConfigBuilder/RelationMappingEditor.tsx`

**Interfaces:**
- Consumes：同 Task 5

**方法论**：跟 Task 5 一样——搜 `border-subtle`/`border-status-error`/`shadow-soft`/`shadow-soft-sm` 定位候选元素，按角色套用圆角档位。这一批文件普遍偏小，多数文件只有 1-3 个候选元素。特别提示几个语义判断：

- `TaskStatusBadge.tsx` 渲染的是徽章 → `rounded-chip`
- `ToastContext.tsx` 渲染的 toast 提示条 → `rounded-panel`（spec 明确写了 toast 归在 panel 档）
- `ConfirmContext.tsx` 渲染的确认弹窗 → `rounded-modal`
- `Tooltip.tsx` 渲染的提示条 → `rounded-modal`（虽然视觉上很小，但语义上是浮层，不是常驻控件，spec 里明确跟确认弹窗归在同一档）
- `Skeleton.tsx` 的两种占位容器（`table-rows`/`card-list`）→ `rounded-card`（骨架屏形状要跟它占位的真实内容——表格外框/列表卡片——保持一致，不能加载态是圆角、加载完是别的圆角，或者反过来）
- `SkinSwitcher.tsx`/`DensitySwitcher.tsx`/`TenantSwitcher.tsx` 里的 `<select>` → `rounded-control`
- `Pager.tsx` 的页码按钮 → `rounded-control`
- `CopyButton.tsx` → `rounded-control`

- [ ] **Step 1-16**: 按上面的文件列表逐一处理（大多数文件改动量很小，1-2 处圆角）

- [ ] **Step 17: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 18: Commit**

```bash
git add frontend/src/admin
git commit -m "feat(design): apply border-radius tokens to admin shared components"
```

---

### Task 7: 圆角落地 —— 前台聊天页 + 布局微调

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx`
- Modify: `frontend/src/components/ChatWindow.tsx`
- Modify: `frontend/src/components/ChatSidebar.tsx`
- Modify: `frontend/src/components/ChatInput.tsx`
- Modify: `frontend/src/components/MessageBubble.tsx`
- Modify: `frontend/src/components/SourceCitations.tsx`
- Modify: `frontend/src/components/Hero.tsx`
- Modify: `frontend/src/components/Footer.tsx`
- Modify: `frontend/src/components/MarkdownContent.tsx`

**Interfaces:**
- Consumes：同 Task 5

**方法论**：同 Task 5/6。补充语义判断：

- `MessageBubble.tsx` 的消息气泡 → `rounded-card`
- `SourceCitations.tsx` 的引用标签 → `rounded-chip`
- `ChatInput.tsx` 的输入框 → `rounded-control`
- `ChatSidebar.tsx` 的会话列表项按钮 → `rounded-control`（这是按钮不是卡片，虽然视觉上排成列表，但交互上是可点击的导航项，跟其它 `<button>` 同档）
- `MarkdownContent.tsx` 已有的孤立 `rounded`（行内代码块）——检查它跟新体系是否冲突：如果 Task 1 的 `borderRadius.DEFAULT` 改成了 `8px`，这个已有的 `rounded` class 会自动跟着变成 8px（Tailwind 的 `rounded` 就是 `borderRadius.DEFAULT`），不需要手动改这一处，只需要确认改完之后视觉上还合理（8px 对一个行内代码块来说是可以接受的值，不需要跟其它任何东西保持不一致的处理）
- `ChatWindow.tsx` 最外层容器（如果读代码发现有整体外框）→ `rounded-container`；如果没有明显的"整体外框"元素就跳过，不要为了套用而生造一个不存在的容器

**布局微调**：

- [ ] **Step 1: `frontend/src/pages/ChatPage.tsx`** —— 聊天消息列容器的 `max-w-3xl` 改成 `max-w-4xl`（先读代码确认这个 class 出现的确切位置，再改；这是本任务里唯一的非圆角改动）

**圆角落地**：

- [ ] **Step 2-9**: 按上面的文件列表逐一处理其余 8 个文件

- [ ] **Step 10: 验证类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无输出

- [ ] **Step 11: Commit**

```bash
git add frontend/src/pages frontend/src/components
git commit -m "feat(design): apply border-radius tokens to chat UI, widen message column"
```

---

## 手工验证（全部任务完成后，浏览器里逐项确认）

1. 打开前台聊天页和后台任意一个列表页，肉眼确认：按钮/卡片/表单/弹窗都有圆角，且大致符合"越大的容器圆角越大"的层次感（按钮 7px 明显比表单区块 14px 更方正）。
2. 确认所有边框变细、变成半透明灰色调，不再是纯黑 2px 实边；错误提示（比如故意触发一次表单校验错误）的边框应该保持红色但也是细边。
3. 确认所有原本的硬直角偏移阴影都变成柔和模糊阴影（卡片周围应该有一圈渐变模糊的灰影，不是直角硬边的小方块阴影）。
4. 点击任意一个按钮，确认按下时是缩小+轻微变透明，不再是位移+阴影消失。
5. 切换三套皮肤，确认圆角/阴影层数/边框宽度视觉上完全一致，只有颜色深浅跟着皮肤变（暗色皮肤下阴影应该更明显，因为提高了透明度补偿深色背景）。
6. 前台聊天页消息列在宽屏（比如 1440px 宽窗口）下应该比之前更宽一些（896px vs 768px）。
7. `prefers-reduced-motion` 场景下（DevTools 模拟），点击按钮的缩放+透明度反馈应该被现有的 reduced-motion 处理逻辑覆盖到（本任务没有新增强制性动画，`active:scale-95` 本身是即时状态切换不是持续动画，不需要额外的 `motion-reduce` 守卫，但如果某个按钮外层已经有 `transition` 且没有 `motion-reduce:transition-none`，属于历史遗留，不在本次任务修复范围）。
