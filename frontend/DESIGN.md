# 前端设计规范

> 本文档是当前 `frontend/` 视觉与交互实现的权威参考。后续新增页面、组件或调整现有样式，
> 都应先查这份文档确认 token/模式是否已存在，避免重复发明或引入不一致的视觉语言。
> 如果某次改动引入了新 token 或新组件模式，请同步更新本文档对应章节。

## 1. 设计语言

**Neo-brutalism（新粗野主义）**，风格来源于对 `raft.build/zh-cn` 的实测抓取（`getComputedStyle`
读取真实渲染样式，非主观模仿）。核心特征：米白背景、纯黑硬边框、无模糊硬投影、全局直角、
糖果色色块、按下有位移触感的微交互。**不是**安静科技感/深色系/圆角卡片风格——如果新增设计
出现圆角卡片、渐变、模糊阴影，说明偏离了本设计语言，需要重新对齐。

历史沿革：项目最早的设计文档（`docs/superpowers/specs/2026-08-07-frontend-demo-design.md`）
基于文本推断得出深色科技风方案，后被 `docs/superpowers/specs/2026-08-07-frontend-raft-visual-redesign-design.md`
的实测结果推翻并替换。本文档是这次替换后持续演进的**当前状态快照**，比那两份历史设计文档更新、更准确。

## 2. 色彩系统

定义于 `tailwind.config.ts` 的 `theme.extend.colors`：

| Token | 值 | 用途 |
|---|---|---|
| `paper` | `#FFFAEF` | 页面主背景（米白，不是纯白） |
| `ink` / `ink-DEFAULT` | `#141111` | 主文字、边框、投影色（近黑，不是纯黑 `#000`） |
| `ink-soft` | `#5C5750` | 次要文字（说明文字、占位符、时间戳类内容） |
| `card` | `#FFFFFF` | 卡片/气泡底色，在 `paper` 之上分层用纯白做区分 |
| `accent-pink` | `#FE7DA8` | 主强调色：主要 CTA 按钮、用户消息气泡 |
| `accent-yellow` | `#FFD440` | 标签/高亮/导航栏背景（黄色导航栏是首屏识别度的关键色块） |
| `accent-cyan` | `#27CCF3` | 链接、输入框 focus 态边框 |
| `accent-green` | `#A9D877` | 成功态（=`status-success`） |
| `accent-orange` | `#F8A16F` | 备用强调色，暂无固定用途，新增强调场景时按需取用 |
| `status-error` | `#F97264` | 错误态文字/边框（珊瑚红，不用标准红 `#EF4444`） |
| `status-success` | `#A9D877` | 成功态，复用 `accent-green` |

**取色原则**：新增 UI 元素需要强调色时，从上表中选，不要引入表外新色号。如果确实需要一个
新语义色（比如"警告态"），先在 `tailwind.config.ts` 里加 token 再用，不要在 className 里
写字面量十六进制色值。

未使用的实测色：raft.build 页面里出现过 `#BBAFE6`（紫）但当前项目暂无使用场景，未收录为
token；如果后续需要第 7 种强调色可以启用它。

## 3. 字体系统

两套字体，都通过 `@fontsource` 本地打包（不发外部网络请求），在 `main.tsx` 引入：

```tsx
import '@fontsource/space-grotesk/400.css'
import '@fontsource/space-grotesk/700.css'
import '@fontsource/space-mono/400.css'
import '@fontsource/space-mono/700.css'
```

对应 `tailwind.config.ts` 的 `fontFamily`：

| Token | 字体栈 | 用途 |
|---|---|---|
| `font-sans`（默认） | `"Space Grotesk", system-ui, "PingFang SC", "Microsoft YaHei", sans-serif` | 全站默认正文/标题。西文字符（英文、数字、Logo）落在 Space Grotesk，中文自动 fallback 到系统黑体 |
| `font-mono` | `"Space Mono", ui-monospace, "SFMono-Regular", monospace` | **仅用于**全大写 + 字距拉宽（`uppercase tracking-widest`）的标签类文字：公告条、footer 分类小标题。不用于正文 |

**字重规则**：标题统一 `font-bold`（700）或更粗；正文保持常规字重（400），不要在正文大段文字上用 `font-bold`。

## 4. 边框与投影系统（brutalism 核心）

`tailwind.config.ts` 的 `boxShadow` 扩展：

```ts
boxShadow: {
  brutal: '2px 2px 0 0 #141111',
  'brutal-sm': '1px 1px 0 0 #141111',
}
```

配套组合类（写在组件 className 里，非 Tailwind 配置）：

- **静止态**：常规元素用 `border-2 border-ink shadow-brutal`；小元素（标签、次级按钮）用
  `border border-ink shadow-brutal-sm` 或 `border-2 ... shadow-brutal-sm`。
- **交互元素按下态**（按钮类必须有）：
  ```
  active:translate-x-[2px] active:translate-y-[2px] active:shadow-none
  ```
  小尺寸按钮（如 nav 里的次级按钮）用 1px 位移：`active:translate-x-px active:translate-y-px`。
  阴影方向位移 + 阴影消失，模拟"按下去"的物理触感，这是全站按钮类元素的标志性微交互，**新增
  任何可点击的按钮都要带这个效果**，不能只做 `:hover` 变色了事。
- **投影颜色**统一用 `#141111`（即 `ink`），不单独配色；偏移量只有 `brutal`/`brutal-sm` 两档，
  不要自造第三档。

## 5. 圆角规则

**全局直角**（`border-radius: 0`），不使用任何自定义圆角 token（历史上删除过 `rounded-card`/
`rounded-bubble`）。

例外（允许保留 Tailwind 内置 `rounded-full`）：
- 纯装饰性的圆形元素，如 `ThinkingIndicator` 的三个跳动点
- 未来如果加头像类圆形图片，也可以用 `rounded-full`

卡片、按钮、输入框、标签/徽章**一律直角**，不允许例外。

## 6. 组件模式清单

以下模式已在 `frontend/src/` 落地，新增同类元素时直接复用对应 className 组合，不要另起风格。

### 6.1 顶部公告条（`ChatPage.tsx`）
黑底 + 黄字 + 等宽字体，用于放置一句话状态说明：
```
border-b-2 border-ink bg-ink px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-accent-yellow
```
内容要求**真实、不夸大**——这是给内部演示环境用的状态条，不是营销 CTA，不写"重磅发布"之类的话术。

### 6.2 导航栏（`ChatPage.tsx`）
黄色背景 + 黑色底边框，是首屏色彩识别的关键区块：
```
flex items-center justify-between border-b-2 border-ink bg-accent-yellow px-6 py-4
```

### 6.3 主按钮（Primary Button）
实心强调色 + 硬投影，用于最主要的操作（发送消息）：
```
border-2 border-ink bg-accent-pink px-5 py-2.5 font-bold text-ink shadow-brutal
transition active:translate-x-[2px] active:translate-y-[2px] active:shadow-none
disabled:cursor-not-allowed disabled:opacity-50
```
参考实现：`ChatInput.tsx` 发送按钮。

### 6.4 次级按钮（Secondary / Outline Button）
白/米白底 + 黑边框，无强调色填充，用于非主线操作（重置、取消一类）：
```
min-h-[44px] cursor-pointer border-2 border-ink bg-paper px-3 py-1.5 text-sm font-bold text-ink shadow-brutal-sm
transition active:translate-x-px active:translate-y-px active:shadow-none
disabled:cursor-not-allowed disabled:opacity-50
```
参考实现：`ChatPage.tsx` 导航栏"重新开始对话"按钮。**判断主/次的原则**：一个界面区域如果同时
出现两个可点击操作，优先级更高的用主按钮样式，优先级低的用次级样式，不要两个都用实心色块
（会分不清主次）。`min-h-[44px]` 和 `cursor-pointer` 是所有按钮类元素的强制要求，见第 7 节。

### 6.5 消息气泡（`MessageBubble.tsx`）
- 用户气泡：`border-ink bg-accent-pink text-ink`（深色字，不用白字——粉底配黑边框语境下白字
  对比度不够）
- 助手气泡（正常）：`border-ink bg-card text-ink`
- 助手气泡（错误态）：`border-status-error bg-card text-ink`（**边框**用 `status-error`
  标识错误，**文字**仍用 `ink` 不用 `status-error`——`status-error` 压在白色 `card` 上文字
  对比度只有约 2.7:1，达不到 WCAG AA 正文 4.5:1 门槛。错误态靠边框颜色 + 文案内容传达语义，
  不靠低对比度的红字，符合"不能只靠颜色传达信息"的可访问性原则）
- 公共部分：`max-w-[75%] border-2 px-4 py-3 shadow-brutal`

### 6.6 加载指示器（`ThinkingIndicator`，`MessageBubble.tsx`）
三个跳动圆点，`bg-ink-soft` + `animate-bounce motion-reduce:animate-none` + 递增
`animation-delay`，是圆角规则里明确允许的例外。`motion-reduce:animate-none` 是必须的——
系统开启"减少动态效果"时要降级为静态，不能无条件循环动画。

### 6.7 引用标签 / 徽章（`SourceCitations.tsx`）
直角小方块，黄底黑字 + 小号硬投影：
```
border border-ink bg-accent-yellow px-2.5 py-1 text-xs text-ink shadow-brutal-sm
```
这是通用的"badge"模式，未来如果要展示状态标签、分类标签，复用这个模式（换背景色即可，
边框/投影/直角规则不变）。

### 6.8 输入框（`ChatInput.tsx`）
```
border-2 border-ink bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft
focus:shadow-brutal focus:outline-none disabled:opacity-50
```
focus 态用 `shadow-brutal` 呈现（复用第 4 节的硬投影 token），不用默认的模糊 outline/ring，
也**不要**用强调色（如 `accent-cyan`）改边框色作为 focus 指示——实测 `accent-cyan` 在
`paper` 背景上对比度只有约 1.8:1，低于 WCAG 2.2 非文字元素 3:1 的门槛，`shadow-brutal`
用的是 `ink`，对比度足够且和按下态视觉语言一致。

### 6.9 Footer（`Footer.tsx`）
黑底 + 60% 透明度黄字，与页面主体的米白背景形成首尾对比：
```
border-t-2 border-ink bg-ink px-6 py-8 text-accent-yellow/60
```
分类小标题/说明文字用 `font-mono text-xs uppercase tracking-widest`；品牌名本身
（不透明）用 `text-accent-yellow`（不带 `/60`）以示强调。

### 6.10 管理后台侧边栏导航项（`AdminLayout.tsx`）

未激活态：`border-2 border-ink px-3 py-2.5 text-sm font-bold bg-paper text-ink`；
激活态（当前路由）：`bg-accent-pink text-ink shadow-brutal-sm`（复用主按钮的强调色，
不新增颜色 token）。用 react-router 的 `NavLink` 的 `isActive` 判断，不手动比较路径字符串。

## 7. 交互规范

- **按下反馈**：所有按钮类元素必须有 `active:translate-*` + `active:shadow-none` 组合，见第 4 节。
- **禁用态**：统一 `disabled:opacity-50`，可点击元素额外加 `disabled:cursor-not-allowed`。
- **focus 态**：输入类/按钮类元素用 `focus:shadow-brutal focus:outline-none`（复用硬投影
  token，见第 6.8 节），不用浏览器默认 outline，也不用 box-shadow 环形 focus ring（那是
  圆角设计语言的常见做法，和本设计语言的硬边框调性冲突）；**不要**用 `accent-cyan` 之类的
  强调色改边框色做 focus 指示——糖果色系普遍偏浅，对 `paper` 背景的对比度往往不够（实测
  `accent-cyan` 只有约 1.8:1，低于 WCAG 3:1 门槛），`ink` 系的硬投影对比度足够且更安全。
- **触控目标**：所有可点击元素（按钮、链接）最小高度 `min-h-[44px]`，小尺寸按钮也不例外——
  用 `shadow-brutal-sm`/`py-1.5` 做视觉上的"小"，但可点击区域仍要够 44px。
- **鼠标指针**：所有可点击元素加 `cursor-pointer`（原生 `<button>` 在部分浏览器默认不是手型）。
- **过渡**：涉及位移/透明度变化的元素统一加 `transition`（不指定具体 duration/easing，用
  Tailwind 默认值即可，全站保持一致）。
- **动效降级**：无限循环/自动播放的动画（如 `animate-bounce`）必须配 `motion-reduce:animate-none`
  或等效降级，尊重系统"减少动态效果"设置。
- **视口高度**：需要撑满视口高度的容器用 `min-h-dvh`，不用 `min-h-screen`（`100vh`）——移动
  浏览器地址栏收起/展开会让 `100vh` 跳动，`dvh`（动态视口高度）不受影响。

## 8. 明确不采用的部分

以下是 raft.build 原站存在、但本项目**主动排除**的设计元素，新增功能时不要引入，除非有明确
的新需求驱动并同步更新本文档：

- 深色模式（本站只做一套浅色主题，raft.build 本身也没有深色模式）
- 像素风格插画/吉祥物贴纸装饰（旋转悬浮的头像类装饰图形）——这是营销落地页的个性化装饰，
  和"内部客服 demo"的定位不符
- 首屏左文案右预览卡片的分栏布局、频道列表、成员头像等具体业务场景元素（那些是 raft.build
  自己产品形态的展示，与本项目无关）
- 用户评价区域的错落交错卡片布局 + 虚线连接线（当前页面结构没有这类展示型内容区块）
- FAQ 手风琴、定价分段控制器（Segmented Control）、团队卡片顶部色条——当前页面没有对应
  内容区块，如果未来加了 FAQ/定价类页面再引入，引入时应更新本文档对应章节

## 9. 新增组件/页面时的检查清单

1. 颜色只用第 2 节表格里的 token，不写字面量色值。
2. 字体默认继承 `font-sans`；只有全大写标签类文字才用 `font-mono`。
3. 卡片/按钮/输入框全部直角，只有纯装饰圆形元素允许 `rounded-full`。
4. 有边框的元素配硬投影（`shadow-brutal`/`shadow-brutal-sm`），投影色固定 `ink`。
5. 可点击元素必须有按下位移反馈；区分主/次操作时用第 6.3/6.4 节的两种按钮样式。
6. 如果新模式在本文档里没有先例，先想清楚它属于本设计语言的合理扩展、还是该放进第 8 节
   "明确不采用"，拿不准就参照 raft.build 实测效果（可用 Claude in Chrome 重新抓取确认），
   不要凭印象猜测。

## 10. 参考来源

- 实测数据来源：`docs/superpowers/specs/2026-08-07-frontend-raft-visual-redesign-design.md`
  （首次用 Claude in Chrome 对 raft.build/zh-cn 做 `getComputedStyle` 实测的记录）
- 实施记录：`docs/superpowers/plans/2026-08-08-frontend-raft-visual-redesign.md`
- 本文档随代码演进持续更新，如与实际代码不一致，以代码为准并回来修正本文档。
