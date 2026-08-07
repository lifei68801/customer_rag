# 前端体验 Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建一个放在 `frontend/` 下的可交互前端 demo，让用户能对 `/agent/chat` 做流式文字问答体验，视觉设计参考 raft.build 的深色科技风格；同时准备一份示例语料并摄取进后端，让 demo 打开后有真实可问的内容。

**Architecture:** 5 个任务：①准备示例 FAQ 语料并摄取进向量库/图谱；②前端项目脚手架（React + Vite + TypeScript + Tailwind，含 Vite 开发代理，不改动任何后端代码）；③SSE 解析工具与 `useAgentChat` 数据层 hook；④UI 组件（Hero/聊天窗口/消息气泡/来源引用/输入框）与页面组装；⑤端到端联调验证与人工验收清单。

**Tech Stack:** 后端摄取沿用现有 `app.ingestion.main` CLI（无新依赖）；前端 React 18 + Vite 6 + TypeScript 5 + Tailwind CSS 3，零其他运行时依赖。

## Global Constraints

- 本仓库当前在 `dev/0.1` 分支直接工作（非 main/master），不使用隔离 worktree。
- Commit message 格式：一行摘要（`feat:`/`docs:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。commit message 只能包含普通可打印字符，不要出现控制字符或 BOM——写完后用 `git log -1 --format=%B | od -c | head -5` 核实开头没有 BOM 字节（`357 273 277`），涉及函数名/路径时直接写、不要加反斜杠转义。
- **本计划不写自动化测试**（设计文档 §9 已明确：demo 性质，不引入测试框架）。每个前端代码任务的验证方式是 `npx tsc --noEmit`（类型检查通过）和 `npm run build`（构建产物生成成功），不是 pytest 的 RED/GREEN 循环——这是本计划和本次会话之前所有 Python 计划的关键区别，任务里的"Step"不会有"写失败测试"这一步。
- 前端所有改动只落在 `frontend/` 目录下（示例语料例外，落在 `docs/demo-data/`），**不改动任何 `app/` 下的后端 Python 代码**——包括不加 CORS 中间件、不改 SSE 事件字段。
- 设计依据：`docs/superpowers/specs/2026-08-07-frontend-demo-design.md`（已经用户批准，不要偏离其中的机制决策），尤其是明确排除项：不做语音、不做 `/qa` 接入、不做幕后诊断面板、不做租户切换器、不做完整营销落地页、不做浅色模式。
- `tenant_id` 硬编码为 `"demo"`，`user_id` 硬编码为 `"demo-user"`，两者在前端代码里只应该出现在 `src/hooks/useAgentChat.ts` 这一处常量定义，其余地方不要重复硬编码字符串字面量。
- 摄取命令统一用 `.venv/Scripts/python.exe -m app.ingestion.main`（Windows 环境，本仓库自带 `.venv`）。
- Node 相关命令都要在 `frontend/` 目录下执行（`cd frontend` 后再跑 `npm install`/`npm run dev` 等，不要在仓库根目录跑）。

---

### Task 1: 示例演示语料准备与摄取

**Files:**
- Create: `docs/demo-data/faq-login.md`
- Create: `docs/demo-data/faq-error-e502.md`
- Create: `docs/demo-data/faq-billing.md`
- Create: `docs/demo-data/faq-network.md`

**Interfaces:**
- Consumes：现有 `app/ingestion/main.py` CLI（`python -m app.ingestion.main --dir <目录> --tenant-id <租户> --build-graph`），`app/ingestion/chunking.py::chunk_markdown` 按 `## ` 二级标题切分 chunk（一级 `#` 标题及第一个 `##` 之前的所有内容都不会进入任何 chunk，会被直接丢弃、不参与索引——因此每个文件的第一行用 `# ` 一级标题起个文档名只是给人看的，紧接着的内容都要用 `## ` 起头才能被检索到）。
- Produces：无代码接口，产出是向量库/图谱里的可检索内容，供 Task 5 做端到端验证时提问。

- [ ] **Step 1: 创建示例语料文件**

创建 `docs/demo-data/faq-login.md`：

```markdown
# 登录与账号相关问题

## 登录失败提示密码错误怎么办？
如果多次确认密码无误仍提示登录失败，请按以下步骤处理：
1. 点击登录页面的"忘记密码"链接，通过注册邮箱接收重置链接；
2. 重置链接有效期为 30 分钟，超时需要重新发起；
3. 新密码需至少包含 8 位字符，包含大小写字母和数字；
4. 如果连续 5 次输入错误密码，账号会被临时锁定 15 分钟，请耐心等待后重试。

若按上述步骤操作后仍无法登录，请联系人工客服进一步排查账号状态。

## 示例登录模块支持哪些登录方式？
示例登录模块（原名示例认证模块）目前支持三种登录方式：
- 账号密码登录：使用注册邮箱+密码；
- 短信验证码登录：绑定手机号后可通过验证码免密登录；
- 单点登录（SSO）：企业版客户可对接自有身份提供商，实现统一登录。

不同登录方式下的会话有效期一致，均为 7 天免重复登录。
```

创建 `docs/demo-data/faq-error-e502.md`：

```markdown
# 错误码处理指南

## 示例错误码E502（网关超时示例）是什么意思，该怎么处理？
示例错误码E502，也就是网关超时示例，表示客户端请求经过网关转发到后端服务时超时未响应。常见原因和处理步骤：
1. 检查当前网络连接是否稳定，尝试刷新页面重试一次；
2. 如果是批量导入/大文件上传场景触发该错误码，建议将单次请求数据量拆分得更小；
3. 如果错误持续出现超过 10 分钟，说明可能是后端服务临时过载，建议等待 5-10 分钟后重试；
4. 若上述方法均无效，请记录出现时间并联系人工客服，客服会协助排查网关侧日志。

该错误码不会导致已提交的数据丢失，重试是安全的。
```

创建 `docs/demo-data/faq-billing.md`：

```markdown
# 订阅与账单常见问题

## 如何查看和修改当前的订阅套餐？
登录后进入"账户设置 - 订阅管理"页面，可以查看当前套餐名称、计费周期和到期时间。修改套餐分两种情况：
- 升级套餐：立即生效，按剩余天数补差价；
- 降级套餐：在当前计费周期结束后生效，不支持立即降级退款。

所有套餐变更都会在页面上弹出确认提示，并在变更完成后发送邮件通知。

## 账单扣款失败会有什么影响？
如果订阅到期时扣款失败（如银行卡余额不足、卡片过期），系统会：
1. 自动重试扣款，最多重试 3 次，每次间隔 24 小时；
2. 重试期间账号功能不受影响，仍可正常使用；
3. 3 次重试全部失败后，账号会降级为免费版，部分高级功能受限；
4. 补齐支付方式并完成补缴后，账号会在 1 小时内自动恢复原套餐权益。
```

创建 `docs/demo-data/faq-network.md`：

```markdown
# 网络连接问题排查

## 客户端一直提示"网络连接不稳定"怎么办？
这个提示通常和本地网络环境或代理设置有关，建议按顺序排查：
1. 确认设备本身能正常访问其他网站，排除断网可能；
2. 如果使用了公司代理/VPN，尝试临时关闭后重试，部分代理会拦截长连接请求；
3. 检查本地防火墙/安全软件是否拦截了应用的网络请求，需要将其加入信任名单；
4. 移动网络环境下，尝试切换到 Wi-Fi 网络重试；
5. 若排查后仍无法解决，请提供具体的错误发生时间和网络环境（如公司网络/家庭宽带/移动数据），转交技术支持进一步排查。
```

- [ ] **Step 2: 确认后端环境已配置**

在运行摄取之前，确认 `.env` 里已经配置了真实的 `CUSTOMER_RAG_EMBEDDING_*`（摄取需要真实调用 Embedding API）以及 `CUSTOMER_RAG_MILVUS_URI` 指向一个可用的 Milvus 实例。如果 `--build-graph` 也要跑，还需要 `CUSTOMER_RAG_LLM_*` 和 `CUSTOMER_RAG_NEO4J_*` 配置好。这些不是本任务要新增的配置——只是确认已有的 `.env` 是可用的，如果本机没有可用的 Milvus/Neo4j/API Key，跳过 Step 3-4 的实际执行，在报告里如实说明"环境不可用，摄取命令未实际运行，仅创建了语料文件"，不要虚构执行结果。

- [ ] **Step 3: 运行摄取命令**

Run: `.venv/Scripts/python.exe -m app.ingestion.main --dir docs/demo-data --tenant-id demo --build-graph`
Expected: 命令输出 `已摄取 N 个 chunk，来自目录: docs/demo-data（租户: demo）`，`N` 预期是 6（4 个文件，`faq-login.md`/`faq-billing.md` 各有 2 个 `##` 小节，`faq-error-e502.md`/`faq-network.md` 各有 1 个 `##` 小节，共 2+2+1+1=6 个 chunk）。命令不应该报错退出。

- [ ] **Step 4: 检查人工审核队列没有异常堆积**

Run: `.venv/Scripts/python.exe -m app.graphrag.review_cli list`
Expected: 待审核列表为空，或只有个位数的候选记录（语料里的实体名"示例错误码E502"/"网关超时示例"/"示例登录模块"/"示例认证模块"都直接复用了 `app/graphrag/terminology_seed.yaml` 里已有的标准名/别名，理论上能精确对齐，不应该产生大量未对齐候选）。如果发现大量堆积，说明语料措辞和术语表对不上，需要回头调整 Step 1 的语料文本用词（比如确保错误码/模块名的表述和术语表条目完全一致），不要跳过这个校验直接进入下一个任务。

- [ ] **Step 5: 提交**

```bash
git add docs/demo-data/faq-login.md docs/demo-data/faq-error-e502.md docs/demo-data/faq-billing.md docs/demo-data/faq-network.md
git commit -m "feat: add sample FAQ corpus for frontend demo"
```

（这一步只提交语料文件本身，不提交摄取产生的向量库/图谱数据——那些是运行时状态，不是代码。）

---

### Task 2: 前端项目脚手架

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/tailwind.config.ts`
- Create: `frontend/postcss.config.js`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`（占位版本，Task 4 会替换）
- Create: `frontend/src/styles/index.css`
- Create: `frontend/.gitignore`

**Interfaces:**
- Consumes：无（这是最底层的脚手架任务）。
- Produces：可运行的 Vite 开发环境（`npm run dev` 能启动）、Tailwind 配色 token（`surface.base/raised/card/border`、`accent.DEFAULT/soft`、`content.primary/secondary`、`status.success/error`、`rounded-card`/`rounded-bubble`），Task 3、Task 4 的组件代码会直接使用这些 Tailwind class 名称，颜色 token 命名必须和这里定义的完全一致。

- [ ] **Step 1: 创建 `package.json`**

```json
{
  "name": "customer-rag-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "typecheck": "tsc --noEmit",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.12",
    "@types/react-dom": "^18.3.1",
    "@vitejs/plugin-react": "^4.3.4",
    "autoprefixer": "^10.4.20",
    "postcss": "^8.4.49",
    "tailwindcss": "^3.4.15",
    "typescript": "^5.7.2",
    "vite": "^6.0.3"
  }
}
```

- [ ] **Step 2: 创建 `vite.config.ts`**

```ts
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/agent': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
```

- [ ] **Step 3: 创建 `tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "allowImportingTsExtensions": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src", "vite.config.ts"]
}
```

- [ ] **Step 4: 创建 `tailwind.config.ts`**

```ts
import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          base: '#0A0A0F',
          raised: '#15151F',
          card: '#1B1B29',
          border: '#33333F',
        },
        accent: {
          DEFAULT: '#0EA5E9',
          soft: '#06B6D4',
        },
        content: {
          primary: '#F5F5F7',
          secondary: '#9CA3AF',
        },
        status: {
          success: '#10B981',
          error: '#EF4444',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        card: '12px',
        bubble: '16px',
      },
    },
  },
  plugins: [],
} satisfies Config
```

- [ ] **Step 5: 创建 `postcss.config.js`**

```js
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

- [ ] **Step 6: 创建 `index.html`**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>客服智能问答 Demo</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 7: 创建 `src/styles/index.css`**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

body {
  @apply bg-surface-base text-content-primary font-sans;
}
```

- [ ] **Step 8: 创建 `src/main.tsx`**

```tsx
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
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

- [ ] **Step 9: 创建占位版 `src/App.tsx`**（Task 4 会整体替换成完整聊天界面，这里只是让脚手架能跑起来）

```tsx
function App() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-surface-base text-content-primary">
      <p>前端脚手架就绪，聊天界面将在后续任务接入。</p>
    </div>
  )
}

export default App
```

- [ ] **Step 10: 创建 `.gitignore`**

```
node_modules/
dist/
.vite/
```

- [ ] **Step 11: 安装依赖并验证**

```bash
cd frontend
npm install
```

Expected: 安装成功，无报错（网络问题导致的安装失败需要如实报告，不要假装成功）。

Run: `npm run typecheck`（仍在 `frontend` 目录下）
Expected: 无类型错误输出。

Run: `npm run build`
Expected: 成功生成 `frontend/dist/` 目录，命令退出码为 0。

- [ ] **Step 12: 提交**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/tsconfig.json frontend/tailwind.config.ts frontend/postcss.config.js frontend/index.html frontend/src/main.tsx frontend/src/App.tsx frontend/src/styles/index.css frontend/.gitignore
git commit -m "feat: scaffold frontend project with Vite + React + TypeScript + Tailwind"
```

（`npm install` 会生成 `frontend/package-lock.json`，需要一并提交；`frontend/node_modules/`、`frontend/dist/` 已被 `.gitignore` 排除，不要 `git add -A`。）

---

### Task 3: SSE 解析工具与 `useAgentChat` 数据层

**Files:**
- Create: `frontend/src/lib/sse.ts`
- Create: `frontend/src/hooks/useAgentChat.ts`

**Interfaces:**
- Consumes：Task 2 的项目脚手架（TypeScript/Vite 环境已就绪）。后端 `/agent/chat` 的真实契约（见 `app/api/agent_routes.py`）：`POST` 请求体 `{question, tenant_id, session_id, user_id, voice_response}`；SSE 响应逐行 `data: <JSON>\n\n`，JSON 里 `type` 字段是 `"delta"`（`{type, text}`）、`"audio"`（本次不处理）或 `"final"`（`{type, text, used_sources, audio_segments_base64}`）。
- Produces：
  - `parseSSEStream(response: Response): AsyncGenerator<{ data: string }>`（`frontend/src/lib/sse.ts`）——Task 4 不直接用它，但 Task 4 的组件依赖下面的 hook。
  - `useAgentChat(): { messages: ChatMessage[]; isSending: boolean; sendQuestion: (question: string) => void }`（`frontend/src/hooks/useAgentChat.ts`），`ChatMessage` 类型 `{ id: string; role: 'user' | 'assistant'; text: string; usedSources: string[]; isStreaming: boolean }`——这是 Task 4 所有聊天相关组件的唯一数据来源，字段名和类型必须逐字匹配，因为 Task 4 的 brief 会直接引用这个类型。

- [ ] **Step 1: 创建 `src/lib/sse.ts`**

```ts
export interface ParsedSSEEvent {
  data: string
}

/**
 * 逐块读取 fetch Response 的 body 流，按 SSE 协议的空行分隔符（\n\n）
 * 切出每个事件，提取所有 `data:` 行拼接后返回。后端逐个事件只发一行
 * data（JSON.dumps 默认转义掉了字符串内的换行符），这里按行拼接是为了
 * 兼容 SSE 协议本身允许多行 data 的情况，不是假设后端会这样发。
 */
export async function* parseSSEStream(
  response: Response,
): AsyncGenerator<ParsedSSEEvent> {
  if (!response.body) {
    throw new Error('响应没有可读的 body，无法解析 SSE 流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let separatorIndex = buffer.indexOf('\n\n')
    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex)
      buffer = buffer.slice(separatorIndex + 2)

      const dataLines = rawEvent
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())

      if (dataLines.length > 0) {
        yield { data: dataLines.join('\n') }
      }

      separatorIndex = buffer.indexOf('\n\n')
    }
  }
}
```

- [ ] **Step 2: 创建 `src/hooks/useAgentChat.ts`**

```ts
import { useCallback, useRef, useState } from 'react'
import { parseSSEStream } from '../lib/sse'

const TENANT_ID = 'demo'
const USER_ID = 'demo-user'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  text: string
  usedSources: string[]
  isStreaming: boolean
}

interface AgentDeltaEvent {
  type: 'delta'
  text: string
}

interface AgentFinalEvent {
  type: 'final'
  text: string
  used_sources: string[]
  audio_segments_base64: string[] | null
}

type AgentEvent = AgentDeltaEvent | AgentFinalEvent | { type: string }

function createId(): string {
  return crypto.randomUUID()
}

export function useAgentChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isSending, setIsSending] = useState(false)
  const sessionIdRef = useRef<string>(createId())

  const sendQuestion = useCallback(async (question: string) => {
    const userMessage: ChatMessage = {
      id: createId(),
      role: 'user',
      text: question,
      usedSources: [],
      isStreaming: false,
    }
    const assistantMessageId = createId()
    const assistantMessage: ChatMessage = {
      id: assistantMessageId,
      role: 'assistant',
      text: '',
      usedSources: [],
      isStreaming: true,
    }
    setMessages((prev) => [...prev, userMessage, assistantMessage])
    setIsSending(true)

    try {
      const response = await fetch('/agent/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question,
          tenant_id: TENANT_ID,
          session_id: sessionIdRef.current,
          user_id: USER_ID,
          voice_response: false,
        }),
      })

      if (!response.ok) {
        throw new Error(`后端返回状态码 ${response.status}`)
      }

      for await (const event of parseSSEStream(response)) {
        const parsed = JSON.parse(event.data) as AgentEvent

        if (parsed.type === 'delta') {
          const delta = parsed as AgentDeltaEvent
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantMessageId
                ? { ...message, text: message.text + delta.text }
                : message,
            ),
          )
        } else if (parsed.type === 'final') {
          const final = parsed as AgentFinalEvent
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantMessageId
                ? {
                    ...message,
                    text: final.text,
                    usedSources: final.used_sources ?? [],
                    isStreaming: false,
                  }
                : message,
            ),
          )
        }
      }
    } catch (error) {
      const detail = error instanceof Error ? error.message : '未知错误'
      setMessages((prev) =>
        prev.map((message) =>
          message.id === assistantMessageId
            ? {
                ...message,
                text: `连接后端失败：${detail}，请确认服务已启动。`,
                isStreaming: false,
              }
            : message,
        ),
      )
    } finally {
      setIsSending(false)
    }
  }, [])

  return { messages, isSending, sendQuestion }
}
```

注意：`final` 事件到达时用 `final.text` **整体覆盖** `message.text`（不是追加）——这是刻意的，对应后端 `app/api/agent_routes.py::agent_chat_endpoint` 文档字符串里明确说的"`final` 事件是权威的最终结果……如果完整语义安全审查事后判定不安全，`final` 事件里的 text 会是兜底话术，可能与之前推送的增量内容不一致"，用 `final.text` 覆盖能正确处理这种"流式内容被事后替换"的场景，不要改成追加逻辑。

- [ ] **Step 3: 验证**

Run（在 `frontend` 目录下）: `npm run typecheck`
Expected: 无类型错误。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/lib/sse.ts frontend/src/hooks/useAgentChat.ts
git commit -m "feat: add SSE parser and useAgentChat data hook"
```

---

### Task 4: UI 组件与页面组装

**Files:**
- Create: `frontend/src/components/Hero.tsx`
- Create: `frontend/src/components/MessageBubble.tsx`
- Create: `frontend/src/components/SourceCitations.tsx`
- Create: `frontend/src/components/ChatWindow.tsx`
- Create: `frontend/src/components/ChatInput.tsx`
- Modify: `frontend/src/App.tsx`（替换 Task 2 的占位版本）

**Interfaces:**
- Consumes：Task 3 的 `useAgentChat()` hook 和 `ChatMessage` 类型（从 `../hooks/useAgentChat` 导入）；Task 2 定义的 Tailwind 颜色 token（`surface-base`/`surface-raised`/`surface-card`/`surface-border`/`accent`/`accent-soft`/`content-primary`/`content-secondary`/`rounded-card`/`rounded-bubble`）。
- Produces：完整可交互的聊天界面，这是本计划的最后一个新增代码任务，后续任务（Task 5）不依赖这里的内部实现细节，只做端到端验证。

- [ ] **Step 1: 创建 `src/components/SourceCitations.tsx`**

```tsx
interface SourceCitationsProps {
  sources: string[]
}

export function SourceCitations({ sources }: SourceCitationsProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2 border-t border-surface-border pt-2">
      {sources.map((source) => (
        <span
          key={source}
          className="rounded-full bg-surface-raised px-2.5 py-1 text-xs text-content-secondary"
        >
          📄 {source}
        </span>
      ))}
    </div>
  )
}
```

- [ ] **Step 2: 创建 `src/components/MessageBubble.tsx`**

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
        className={`max-w-[75%] rounded-bubble px-4 py-3 ${
          isUser
            ? 'bg-accent text-white'
            : 'border border-surface-border bg-surface-card text-content-primary'
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
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary [animation-delay:-0.3s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary [animation-delay:-0.15s]" />
      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-content-secondary" />
    </div>
  )
}
```

- [ ] **Step 3: 创建 `src/components/ChatWindow.tsx`**

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
      <div className="flex flex-1 items-center justify-center px-4 text-center text-content-secondary">
        输入问题开始体验，比如"网关超时示例是什么意思？"
      </div>
    )
  }

  return (
    <div className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-6">
      {messages.map((message) => (
        <MessageBubble key={message.id} message={message} />
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
```

- [ ] **Step 4: 创建 `src/components/ChatInput.tsx`**

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
      className="flex items-center gap-3 border-t border-surface-border bg-surface-raised px-4 py-4"
    >
      <input
        type="text"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="输入你的问题…"
        disabled={disabled}
        className="flex-1 rounded-card border border-surface-border bg-surface-base px-4 py-2.5 text-content-primary placeholder:text-content-secondary focus:border-accent focus:outline-none disabled:opacity-50"
      />
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="rounded-card bg-accent px-5 py-2.5 font-medium text-white transition hover:bg-accent-soft disabled:cursor-not-allowed disabled:opacity-50"
      >
        发送
      </button>
    </form>
  )
}
```

- [ ] **Step 5: 创建 `src/components/Hero.tsx`**

```tsx
export function Hero() {
  return (
    <header className="border-b border-surface-border px-6 py-10 text-center">
      <h1 className="text-3xl font-semibold text-content-primary sm:text-4xl">
        基于知识图谱增强的企业客服问答 Agent
      </h1>
      <p className="mx-auto mt-3 max-w-xl text-content-secondary">
        检索增强生成、术语知识图谱、多轮对话记忆——向下方输入框提问，实际体验这套系统的推理能力。
      </p>
    </header>
  )
}
```

- [ ] **Step 6: 替换 `src/App.tsx`**

把 Task 2 创建的占位版本整个替换为：

```tsx
import { Hero } from './components/Hero'
import { ChatWindow } from './components/ChatWindow'
import { ChatInput } from './components/ChatInput'
import { useAgentChat } from './hooks/useAgentChat'

function App() {
  const { messages, isSending, sendQuestion } = useAgentChat()

  return (
    <div className="flex min-h-screen flex-col bg-surface-base">
      <nav className="flex items-center justify-between border-b border-surface-border px-6 py-4">
        <span className="font-semibold text-content-primary">客服智能问答 Demo</span>
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

- [ ] **Step 7: 验证**

Run（在 `frontend` 目录下）: `npm run typecheck`
Expected: 无类型错误。

Run: `npm run build`
Expected: 构建成功，退出码 0。

- [ ] **Step 8: 提交**

```bash
git add frontend/src/components/Hero.tsx frontend/src/components/MessageBubble.tsx frontend/src/components/SourceCitations.tsx frontend/src/components/ChatWindow.tsx frontend/src/components/ChatInput.tsx frontend/src/App.tsx
git commit -m "feat: add chat UI components and assemble the demo page"
```

---

### Task 5: 端到端联调验证

**Files:** 无新增文件——这是纯验证任务，产出是验证结果和（如有必要的）小范围修复，不是新代码。

**Interfaces:**
- Consumes：全部前 4 个任务的产出。
- Produces：无（终止任务）。

- [ ] **Step 1: 启动后端**

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

（可以用 `run_in_background` 方式启动，后续步骤要用它。）Expected: 日志显示 `Application startup complete`，无报错退出。

- [ ] **Step 2: 启动前端开发服务器**

```bash
cd frontend
npm run dev -- --port 5173
```

（同样后台启动。）Expected: 输出 `Local: http://localhost:5173/`。

- [ ] **Step 3: 冒烟检查——页面能正常返回**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:5173/`
Expected: `200`

- [ ] **Step 4: 冒烟检查——代理转发到后端生效**

Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:5173/health`
Expected: `200`（这条请求经 Vite 代理转发到后端的 `/health`，能验证 `vite.config.ts` 里的 `proxy` 配置确实生效，不是死配置）。

- [ ] **Step 5: 冒烟检查——`/agent/chat` 经代理可达且返回 SSE**

Run: `curl -s -N -X POST http://localhost:5173/agent/chat -H "Content-Type: application/json" -d "{\"question\": \"网关超时示例是什么意思？\", \"tenant_id\": \"demo\", \"session_id\": \"smoke-test\", \"user_id\": \"demo-user\", \"voice_response\": false}" --max-time 30`

Expected: 输出若干行 `data: {...}` 格式的 SSE 事件，最后一条 `type` 为 `"final"`，且 `text` 字段里的内容与 Task 1 摄取的"网关超时示例/示例错误码E502"语料相关（不是"未找到确切答案，已转人工"这类兜底话术——如果命中的是兜底话术，说明 Task 1 的摄取没有生效或检索没有命中，需要回头排查，不要当作验证通过）。

这一步是本计划唯一一次真正端到端验证"前端能拿到后端基于示例语料生成的真实回答"，如果这一步失败，问题可能出在 Task 1（语料未摄取成功/摄取到了错误的租户）、Task 2（代理配置错误）或 Task 3（请求体字段名拼错），需要根据具体报错定位到对应任务，不要在这一步直接改代码糊弄过去——如果确实需要改动前几个任务产出的文件，视为该任务的 bug 修复，补一条 commit，并在报告里说明改了什么、为什么。

- [ ] **Step 6: 停止后台进程**

停掉 Step 1、Step 2 启动的后台进程，避免遗留占用端口的进程。

- [ ] **Step 7: 记录人工验收清单**

由于本计划的实现者是代码 agent，没有真实浏览器可以做视觉验收，Step 3-5 的 curl 检查只能验证"链路通、数据对"，不能验证"界面好不好看、交互顺不顺"。请在任务报告里明确列出以下清单，交给人工在浏览器里逐项确认（不需要在本任务里执行，只需要把清单写清楚）：

1. 打开 `http://localhost:5173`，Hero 区标题/副标题正常显示，深色背景+青蓝色调是否符合预期。
2. 输入"网关超时示例是什么意思？"，观察回答是否有逐句流式出现的效果（不是一次性蹦出全部文字）。
3. 回答下方是否出现来源引用标签——实际格式是 `used_sources` 原样展示，形如"📄 docs\demo-data\faq-error-e502.md#0"（含目录前缀、Windows 路径分隔符、chunk 序号，不是精简过的纯文件名），这是符合预期的正常格式，不要误判为异常。
4. 连续追问一个指代不明的问题（如先问"E502 怎么处理"，再问"那这个问题会不会丢数据"），观察多轮对话是否连贯（依赖后端记忆/指代消解能力）。
5. 关掉后端进程后再发一条消息，确认界面显示"连接后端失败"的提示，而不是卡死无响应。
6. 用浏览器开发者工具的移动端模拟视图，确认页面在窄屏下没有明显的布局错乱（本计划没有专门做响应式适配任务，这里只是留意有没有严重问题，不是要求完美适配）。

- [ ] **Step 8: 若发现问题，判断是否需要新的 commit**

如果 Step 1-5 的验证发现代码 bug（不是环境问题），修复后单独提交：

```bash
git add <修复涉及的文件>
git commit -m "fix: <具体修复内容>"
```

如果没有发现需要代码修复的问题，这一步无需操作。

---

## 完成后

`frontend/` 目录下有一个可通过 `npm run dev` 启动的 React + Vite + TypeScript + Tailwind 项目，深色科技风格视觉参考 raft.build，能对 `/agent/chat` 做流式文字问答体验并展示来源引用；`docs/demo-data/` 下的示例语料已摄取进后端，demo 打开后能问出有意义的回答。后端代码没有任何改动。人工验收清单已在 Task 5 的报告里留档，需要人工打开浏览器逐项确认视觉/交互细节。
