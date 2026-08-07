# 前端视觉重设计（对齐 raft.build）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `frontend/` 现有的深色/圆角/淡阴影视觉方案，替换为实测自 raft.build/zh-cn 的 neo-brutalism 视觉语言（米白背景、纯黑硬边框、无模糊硬投影、直角、糖果色色块、Space Grotesk 西文字体）。

**Architecture:** 纯样式层重构，不改任何组件的 props、状态逻辑或数据流。先改 Tailwind 配置（新色板/字体/阴影 token 的单一定义源），再逐个替换消费这些 token 的组件的 className。

**Tech Stack:** React + Vite + TypeScript + Tailwind CSS 3；新增 `@fontsource/space-grotesk`（本地打包字体，无外部网络请求）。

## Global Constraints

- 所有改动限制在 `frontend/` 目录内，不触碰后端代码（对应设计文档 [[2026-08-07-frontend-raft-visual-redesign-design]] 第 6 节）。
- 本项目前端不使用自动化测试框架（[[2026-08-07-frontend-demo-design]] 第 9 节已确认）；每个任务的验证手段是 `npm run typecheck`（`frontend/` 目录下执行），最终任务用真实浏览器人工核验视觉效果。
- 不改变任何组件的 TypeScript 接口（props、`ChatMessage` 类型等），只改 JSX 的 `className` 字符串和新增的 CSS/字体导入。
- 全站直角（`border-radius: 0`），删除 `rounded-card` / `rounded-bubble` 自定义 token；不引入新图标库，继续用现有 emoji（📄）。
- 硬投影颜色统一用 `#141111`（即 `ink` token），偏移量固定 `2px 2px 0 0`（常规元素）或 `1px 1px 0 0`（小元素，如引用标签）。

---

### Task 1: 引入 Space Grotesk 字体依赖

**Files:**
- Modify: `frontend/package.json`（`dependencies` 块）
- Modify: `frontend/src/main.tsx:1-5`

**Interfaces:**
- Produces: 全局可用的 `Space Grotesk` 字体文件（400/700 字重），后续任务的 Tailwind `fontFamily.sans` 配置依赖它已被加载。

- [ ] **Step 1: 安装依赖**

在 `frontend/` 目录下执行：

```bash
npm install @fontsource/space-grotesk
```

- [ ] **Step 2: 确认 package.json 中新增了依赖**

`frontend/package.json` 的 `dependencies` 块应变为（版本号以 `npm install` 实际写入的为准，不要手动改成下面示意的具体版本号）：

```json
  "dependencies": {
    "@fontsource/space-grotesk": "^5.1.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
```

- [ ] **Step 3: 在入口文件导入字体 CSS**

编辑 `frontend/src/main.tsx`，在现有 import 之后、`import './styles/index.css'` 之前插入两行字体导入：

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/700.css'
import './styles/index.css'

const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('未找到 #root 挂载节点')
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
```

- [ ] **Step 4: 类型检查**

在 `frontend/` 目录下执行：

```bash
npm run typecheck
```

Expected: 无报错（PASS，无输出或仅显示 tsc 正常退出）。

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/main.tsx
git commit -m "feat(frontend): add Space Grotesk font dependency"
```

---

### Task 2: 重写 Tailwind 配色/字体/阴影 token

**Files:**
- Modify: `frontend/tailwind.config.ts`（整个 `theme.extend` 块）

**Interfaces:**
- Consumes: 无（配置文件是所有后续任务的基础）。
- Produces: 后续任务使用的所有 className token：
  - 颜色：`paper`、`ink`（`DEFAULT` + `soft` → `bg-ink`/`text-ink`/`border-ink` 与 `bg-ink-soft`/`text-ink-soft`）、`card`、`accent-pink`、`accent-yellow`、`accent-cyan`、`accent-green`、`accent-orange`、`status-success`、`status-error`
  - 字体：`font-sans`（西文走 Space Grotesk，中文 fallback 系统黑体）
  - 阴影：`shadow-brutal`、`shadow-brutal-sm`
  - `borderRadius` 自定义 token（`card`/`bubble`）被移除，不再产出。

- [ ] **Step 1: 替换 tailwind.config.ts 全文**

把 `frontend/tailwind.config.ts` 整个文件内容替换为：

```ts
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
          error: '#F97264',
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

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS（此步骤只改了配置文件，不影响 TS 类型系统，但作为习惯性检查确认没有连带破坏）。

- [ ] **Step 3: Commit**

```bash
git add frontend/tailwind.config.ts
git commit -m "feat(frontend): replace dark rounded palette with raft.build brutalist tokens"
```

---

### Task 3: 更新全局样式（index.css）

**Files:**
- Modify: `frontend/src/styles/index.css`

**Interfaces:**
- Consumes: Task 2 产出的 `paper`、`ink`、`font-sans` token。
- Produces: `body` 默认背景/文字色，后续所有页面内容继承此基线。

- [ ] **Step 1: 替换 index.css 全文**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

body {
  @apply bg-paper text-ink font-sans;
}
```

- [ ] **Step 2: 启动开发服务器确认无 CSS 编译错误**

在 `frontend/` 目录下执行（若已有实例在跑，跳过启动，直接看终端有无报错）：

```bash
npm run dev
```

Expected: Vite 正常启动，无 PostCSS/Tailwind 报错（终端出现 `Local: http://localhost:5173/` 之类的正常输出）。启动后可按 Ctrl+C 停止，本步骤只是确认编译通过。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/styles/index.css
git commit -m "feat(frontend): apply paper/ink base colors to body"
```

---

### Task 4: 更新 App.tsx（导航栏 + 页面容器）

**Files:**
- Modify: `frontend/src/App.tsx:9-13`

**Interfaces:**
- Consumes: `bg-paper`、`border-ink`、`text-ink`（Task 2）。
- Produces: 无对外接口变化，纯 JSX className 替换。

- [ ] **Step 1: 替换 App.tsx 全文**

```tsx
import { Hero } from './components/Hero'
import { ChatWindow } from './components/ChatWindow'
import { ChatInput } from './components/ChatInput'
import { useAgentChat } from './hooks/useAgentChat'

function App() {
  const { messages, isSending, sendQuestion } = useAgentChat()

  return (
    <div className="flex min-h-screen flex-col bg-paper">
      <nav className="flex items-center justify-between border-b-2 border-ink px-6 py-4">
        <span className="font-bold text-ink">客服智能问答 Demo</span>
      </nav>
      <Hero />
      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col">
        <ChatWindow messages={messages} />
        <ChatInput disabled={isSending} onSend={sendQuestion} />
      </main>
    </div>
  )
}

export default App
```

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/App.tsx
git commit -m "feat(frontend): restyle nav and page shell with brutalist tokens"
```

---

### Task 5: 更新 Hero.tsx

**Files:**
- Modify: `frontend/src/components/Hero.tsx`

**Interfaces:**
- Consumes: `border-ink`、`text-ink`、`text-ink-soft`、`font-bold`（Task 2）。
- Produces: 无对外接口变化。

- [ ] **Step 1: 替换 Hero.tsx 全文**

```tsx
export function Hero() {
  return (
    <header className="border-b-2 border-ink px-6 py-10 text-center">
      <h1 className="text-3xl font-bold text-ink sm:text-4xl">
        基于知识图谱增强的企业客服问答 Agent
      </h1>
      <p className="mx-auto mt-3 max-w-xl text-ink-soft">
        检索增强生成、术语知识图谱、多轮对话记忆——向下方输入框提问，实际体验这套系统的推理能力。
      </p>
    </header>
  )
}
```

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/Hero.tsx
git commit -m "feat(frontend): restyle Hero heading with brutalist tokens"
```

---

### Task 6: 更新 ChatWindow.tsx

**Files:**
- Modify: `frontend/src/components/ChatWindow.tsx:16-21,24-25`

**Interfaces:**
- Consumes: `bg-paper`、`text-ink-soft`（Task 2）。
- Produces: 无对外接口变化。

- [ ] **Step 1: 替换 ChatWindow.tsx 全文**

```tsx
import { useEffect, useRef } from 'react'
import type { ChatMessage } from '../hooks/useAgentChat'
import { MessageBubble } from './MessageBubble'

interface ChatWindowProps {
  messages: ChatMessage[]
}

export function ChatWindow({ messages }: ChatWindowProps) {
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center bg-paper px-4 text-center text-ink-soft">
        输入问题开始体验，比如"网关超时示例是什么意思？"
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-y-auto bg-paper px-4 py-6">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
```

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ChatWindow.tsx
git commit -m "feat(frontend): restyle ChatWindow background with paper token"
```

---

### Task 7: 更新 MessageBubble.tsx（含 ThinkingIndicator）

**Files:**
- Modify: `frontend/src/components/MessageBubble.tsx`

**Interfaces:**
- Consumes: `border-ink`、`bg-accent-pink`、`bg-card`、`border-status-error`、`text-status-error`、`shadow-brutal`、`bg-ink-soft`（Task 2）；`SourceCitations` 组件（Task 8 会改其内部样式，但 props 接口 `sources: string[]` 不变，此任务对它的调用方式不受影响）。
- Produces: 无对外接口变化。

- [ ] **Step 1: 替换 MessageBubble.tsx 全文**

```tsx
import type { ChatMessage } from '../hooks/useAgentChat'
import { SourceCitations } from './SourceCitations'

interface MessageBubbleProps {
  message: ChatMessage
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === 'user'

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] border-2 px-4 py-3 shadow-brutal ${
          isUser
            ? 'border-ink bg-accent-pink text-ink'
            : message.isError
              ? 'border-status-error bg-card text-status-error'
              : 'border-ink bg-card text-ink'
        }`}
      >
        {message.text ? (
          <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>
        ) : message.isStreaming ? (
          <ThinkingIndicator />
        ) : null}
        {!isUser && !message.isStreaming && message.usedSources.length > 0 && (
          <SourceCitations sources={message.usedSources} />
        )}
      </div>
    </div>
  )
}

function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-1 py-1">
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft [animation-delay:-0.3s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft [animation-delay:-0.15s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ink-soft" />
    </div>
  )
}
```

（`ThinkingIndicator` 的三个点保留 `rounded-full`——这是通用 Tailwind 工具类而非被删除的自定义 `rounded-bubble`/`rounded-card` token，圆点装饰不受"全站直角"约束，符合设计文档第 4 节只针对卡片/按钮/输入框的直角要求。）

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/MessageBubble.tsx
git commit -m "feat(frontend): restyle message bubbles with hard borders and offset shadow"
```

---

### Task 8: 更新 SourceCitations.tsx

**Files:**
- Modify: `frontend/src/components/SourceCitations.tsx`

**Interfaces:**
- Consumes: `border-ink`、`bg-accent-yellow`、`text-ink`、`shadow-brutal-sm`（Task 2）。
- Produces: 无对外接口变化（`SourceCitationsProps` 不变）。

- [ ] **Step 1: 替换 SourceCitations.tsx 全文**

```tsx
interface SourceCitationsProps {
  sources: string[]
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-ink pt-2">
      {sources.map((source) => (
        <span
          key={source}
          className="border border-ink bg-accent-yellow px-2.5 py-1 text-xs text-ink shadow-brutal-sm"
        >
          📄 {source}
        </span>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/SourceCitations.tsx
git commit -m "feat(frontend): restyle source citation tags as brutalist chips"
```

---

### Task 9: 更新 ChatInput.tsx（含按下微交互）

**Files:**
- Modify: `frontend/src/components/ChatInput.tsx`

**Interfaces:**
- Consumes: `border-ink`、`bg-card`、`bg-paper`、`text-ink`、`text-ink-soft`、`focus:border-accent-cyan`、`bg-accent-pink`、`shadow-brutal`（Task 2）。
- Produces: 无对外接口变化（`ChatInputProps` 不变）。

- [ ] **Step 1: 替换 ChatInput.tsx 全文**

```tsx
import { useState, type FormEvent } from 'react'

interface ChatInputProps {
  disabled: boolean
  onSend: (question: string) => void
}

export function ChatInput({ disabled, onSend }: ChatInputProps) {
  const [value, setValue] = useState('')

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setValue('')
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex items-center gap-3 border-t-2 border-ink bg-card px-4 py-4"
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入你的问题…"
        disabled={disabled}
        className="flex-1 border-2 border-ink bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft focus:border-accent-cyan focus:outline-none disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none disabled:cursor-not-allowed disabled:opacity-50"
      >
        发送
      </button>
    </form>
  )
}
```

- [ ] **Step 2: 类型检查**

```bash
npm run typecheck
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ChatInput.tsx
git commit -m "feat(frontend): restyle chat input and add brutalist press interaction"
```

---

### Task 10: 完整构建 + 浏览器人工核验

**Files:**
- 无新增/修改文件（纯验证任务）。

**Interfaces:**
- Consumes: 全部前序任务的最终产物。
- Produces: 无（本任务是本计划的终止验证点）。

- [ ] **Step 1: 生产构建检查**

在 `frontend/` 目录下执行：

```bash
npm run build
```

Expected: 构建成功退出（`tsc --noEmit && vite build` 均无报错），产物写入 `frontend/dist/`。

- [ ] **Step 2: 启动前后端并打开浏览器**

后端（仓库根目录）：

```bash
uvicorn app.main:app --reload
```

前端（`frontend/` 目录，另开一个终端）：

```bash
npm run dev
```

用浏览器打开 `http://localhost:5173/`。

- [ ] **Step 3: 视觉核验清单**

逐项确认（对应设计文档 [[2026-08-07-frontend-raft-visual-redesign-design]] 第 7 节）：

1. 页面背景为米白色（`#FFFAEF`），不是白色或深色。
2. 页面标题（Logo、Hero 标题）西文/数字字符使用 Space Grotesk（明显几何感的无衬线字体，非系统默认字体）；打开浏览器 DevTools 的 Elements 面板，选中标题元素，确认 Computed 面板里 `font-family` 第一项生效值是 `Space Grotesk` 而非 fallback。
3. nav 栏、Hero 区底部分隔线为 2px 实心黑线（非 1px 淡灰线）。
4. 发送一条消息：用户气泡为粉色（`#FE7DA8`）+ 黑色 2px 边框 + 右下方向硬投影，文字为深色（非白色）。
5. 助手回复气泡为白色 + 黑色 2px 边框 + 硬投影；若命中术语强制注入或检索失败走 Fallback（可尝试提问"网关超时示例是什么意思？"触发术语条目），错误态应显示珊瑚红（`#F97264`）描边文字。
6. 若回复带来源引用，引用标签应为黄色（`#FFD440`）直角小方块，黑色细边框，文字清晰可读（非灰底浅字）。
7. 用鼠标按住发送按钮不松开：按钮应整体向右下位移 2px 且投影消失（"按下去"的触感）；松开后恢复。
8. 全局检查：所有卡片/按钮/输入框应为直角（无圆角），仅 ThinkingIndicator 加载点和头像类装饰允许保留圆形。
9. Ctrl+F5 强制刷新后重复步骤 2 的字体检查，确认字体是本地打包生效（Network 面板能看到从本地 `/assets/` 路径加载的字体文件，而非请求 fonts.googleapis.com 等外部域名）。

若发现任何一项不符，记录具体元素和问题，返回对应任务的组件文件修正，重新走该任务的类型检查+提交步骤。

- [ ] **Step 4: 停止开发服务器**

核验完成后，Ctrl+C 停止 `npm run dev` 和 `uvicorn` 两个进程（不需要提交任何文件——本任务本身不产生代码改动）。
