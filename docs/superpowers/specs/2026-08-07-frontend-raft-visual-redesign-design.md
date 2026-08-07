# 前端视觉重设计：对齐 raft.build 真实设计语言

> 状态：设计定稿（经用户逐项确认）
> 背景：[[2026-08-07-frontend-demo-design]] 中的视觉方案是基于对 raft.build 的**文本/结构推断**得出的近似值（深色系、青蓝强调色、Inter 字体、圆角卡片、淡阴影），并非真实抓取的 CSS。本次用 Claude in Chrome 实际打开 `https://raft.build/zh-cn/` 并用 `javascript_tool` 读取了渲染后的 computed style（背景色、边框、圆角、box-shadow、font-family），发现真实设计语言是 **neo-brutalism（新粗野主义）**：米白背景、纯黑硬边框、无模糊硬投影、直角、糖果色色块——与已实现的深色/圆角/淡阴影方向完全相反。本设计文档记录重设计方案，实现完成后将替代原设计文档第 1 节（配色）和第 7 节"深色模式"条目的结论。

## 1. 设计依据（实测数据）

通过 `getComputedStyle` 在 raft.build/zh-cn 首页实测得到：

- `body` 背景：`rgb(255, 250, 239)` = `#FFFAEF`（米白，非纯白）
- 标题字体：`"Space Grotesk", system-ui, sans-serif`，`font-weight: 700`
- 按钮/CTA：`background: #FE7DA8`（粉）；`border: 2px solid #141111`（近黑）；`border-radius: 0px`；`box-shadow: 2px 2px 0px 0px #141111`（无模糊硬投影，非淡阴影）
- 页面高频背景色统计（按出现次数排序）：`#141111`（黑，256次，边框/文字为主）、`#FFD440`（黄，64次）、`#F97264`（珊瑚红，62次）、`#27CCF3`（青，33次）、`#9DCAAA`/`#A9D877`（绿，共32次）、`#F8A16F`（橙，22次）、`#FE7DA8`（粉，15次）、`#BBAFE6`（紫，4次）、`#FFFAEF`/`#FFFDF8`（米白系背景）
- 整体调性：糖果色块 + 纯黑描边 + 硬投影 + 直角，是"贴纸/像素风"的活泼调性，不是原文档描述的"安静科技感"

## 2. 色板变更

替换 `frontend/tailwind.config.ts` 的 `colors` 配置：

| Token | 新值 | 原值（deprecated） | 用途 |
|---|---|---|---|
| `paper` | `#FFFAEF` | `surface.base #0A0A0F` | 页面背景 |
| `ink` | `#141111` | `content.primary #F5F5F7` | 主文字、边框、阴影色 |
| `ink-soft` | `#5C5750` | `content.secondary #9CA3AF` | 次要文字（米白底需要比中灰更深的次要色才够对比度） |
| `card` | `#FFFFFF` | `surface.card #1B1B29` | 卡片/气泡底色，在 paper 之上分层 |
| `accent.pink` | `#FE7DA8` | `accent.DEFAULT #0EA5E9` | 主强调色（CTA、用户气泡） |
| `accent.yellow` | `#FFD440` | — | 标签/高亮 |
| `accent.cyan` | `#27CCF3` | `accent.soft #06B6D4` | 链接/focus 态 |
| `accent.green` | `#A9D877` | — | 成功态 |
| `accent.orange` | `#F8A16F` | — | 备用强调 |
| `status.error` | `#F97264` | `status.error #EF4444` | 错误态（实测色比原来的标准红更贴合整体糖果色调） |
| `status.success` | `#A9D877` | `status.success #10B981` | 成功态复用 accent.green |

`borderRadius`：删除 `card: 12px` / `bubble: 16px` 两个 token，全站直角（`0`），不再需要圆角 token。

## 3. 字体变更

- 新增依赖 `@fontsource/space-grotesk`（本地打包，构建时随产物打入，不发外部请求）
- `fontFamily.sans` 改为 `['"Space Grotesk"', 'system-ui', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif']`——西文字符（英文、数字、Logo）落在 Space Grotesk，中文字符自动 fallback 到系统黑体
- 标题类元素统一用 `font-bold`（700）或更粗，与 Raft 标题字重一致；正文保持常规字重

## 4. 边框与投影系统（neo-brutalism 核心）

新增 Tailwind `boxShadow` 扩展 token：

```ts
boxShadow: {
  brutal: '2px 2px 0 0 #141111',
  'brutal-sm': '1px 1px 0 0 #141111',
}
```

配套约定的组合类（非 Tailwind 配置，写在组件里）：
- 静止态：`border-2 border-ink shadow-brutal`（或小元素用 `border border-ink shadow-brutal-sm`）
- 交互元素按下态：`active:translate-x-[2px] active:translate-y-[2px] active:shadow-none`（阴影方向位移 + 阴影消失，模拟"按下去"的物理触感，这是 Raft 按钮的标志性微交互，实测确认存在）

## 5. 组件改造清单

- **`App.tsx`**：容器背景 `bg-surface-base` → `bg-paper`；nav 边框 `border-surface-border` → `border-b-2 border-ink`；Logo 文字用 `font-bold`（西文走 Space Grotesk）
- **`Hero.tsx`**：标题字重升级（`font-semibold` → `font-bold`，与第 3 节字重约定一致），底部分隔线改 `border-b-2 border-ink`；**布局结构不变**，仍是居中标题+副标题（不引入 Raft 首屏的左右分栏预览卡片，原设计文档第 6 节"页面结构"结论保留）
- **`ChatWindow.tsx`**：背景改 `paper`，空状态文字颜色用 `ink-soft`
- **`MessageBubble.tsx`**：
  - 用户气泡：`bg-accent-pink text-ink border-2 border-ink shadow-brutal`，直角（原白字改深色字，因为粉色背景配黑边框语境下白字对比度不够，改深色文字更符合 Raft 全站"浅底深字"的一贯用法）
  - 助手气泡：`bg-card border-2 border-ink shadow-brutal`（错误态用 `border-status-error text-status-error`）
  - `ThinkingIndicator` 三个跳动点颜色改 `bg-ink-soft`
- **`SourceCitations.tsx`**：引用标签从 `rounded-full` 胶囊改为直角小方块 `bg-accent-yellow border border-ink shadow-brutal-sm`，文字色改 `text-ink`（黄底配原来的浅灰字不可读）
- **`ChatInput.tsx`**：
  - input：直角、`border-2 border-ink`，focus 态 `focus:border-accent-cyan`（去掉原来的圆角+青蓝色调焦点环）
  - 发送按钮：`bg-accent-pink border-2 border-ink shadow-brutal` + 第 4 节的按下位移效果，disabled 态降透明度（保留原有 `disabled:opacity-50` 逻辑）

## 6. 明确不做（范围边界）

- 不改整体页面骨架结构（nav / Hero / 聊天区三段式布局保留，仅替换视觉样式），不做 Raft 首屏那种左文案右预览卡片的分栏布局
- 不引入 Raft 首屏的频道列表、成员头像、像素宠物装饰等具体业务元素
- 不做深色模式（Raft 本身也没有深色模式入口，[[2026-08-07-frontend-demo-design]] 原有的"只做深色主题"结论在此被推翻，改为只做此浅色主题）
- 不新增字体之外的其他外部资源依赖（图标继续用现有的 emoji/SVG 方案，不引入图标库）

## 7. 测试与验证方式

沿用原设计文档第 9 节的方式：无自动化测试框架，实施阶段启动前后端后人工核验——重点检查糖果色在米白背景上的可读性对比度（尤其是黄底文字、粉底文字）、按钮按下微交互是否生效、Space Grotesk 是否正确加载（西文字符渲染，非 fallback 系统字体）、中文文案在新配色下的视觉层级是否清晰。
