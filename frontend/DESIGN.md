# 前端设计规范

> 本文档是当前 `frontend/` 视觉与交互实现的权威参考。后续新增页面、组件或调整现有样式，
> 都应先查这份文档确认 token/模式是否已存在，避免重复发明或引入不一致的视觉语言。
> 如果某次改动引入了新 token 或新组件模式，请同步更新本文档对应章节。

## 1. 设计语言

**信标（Console）**——深色技术感基调，颜色只承担"信号"功能（标状态、标强调），不用来营造
情绪。核心特征：深蓝黑基座、细边框分层（不用阴影）、收紧的小圆角、等宽字体贯穿标题与数据
展示、克制的双色信号（青色主信号 + 铜色次信号）。这套语言统一覆盖前台客服聊天页与后台全部
管理页面，不做区分——设计规格见 `docs/superpowers/specs/2026-08-26-console-visual-identity-design.md`，
实施计划见 `docs/superpowers/plans/2026-08-26-console-visual-identity.md`。

历史沿革：本项目最早的设计语言是米白背景+纯黑硬边框+糖果色块的 Neo-brutalism（新粗野主义，
实测抓取自 `raft.build/zh-cn`），2026-08-26 的这次全站重设计将其整体替换为"信标"方向。如果
新增设计出现米白背景、糖果色块、纯直角、硬投影位移，说明还停留在旧设计语言，需要重新对齐。

## 2. 色彩系统

定义于 `frontend/src/styles/index.css` 的 CSS 自定义属性（空格分隔 RGB 三元组，供
`tailwind.config.ts` 用 `rgb(var(--x) / <alpha-value>)` 消费，以支持 `bg-x/40` 这类透明度
修饰符），按三档"皮肤"（skin）分别取值——三档是同一个信标身份的不同亮度，不是三个不同产品：

| Token | 语义 | 用途 |
|---|---|---|
| `paper` | 主背景 | 页面/输入框底色 |
| `ink` / `ink-DEFAULT` | 主文字 | 正文、标题文字 |
| `ink-soft` | 次要文字 | 说明文字、占位符、时间戳 |
| `card` | 卡片/面板表面 | 卡片、弹层、导航栏等分层表面的底色 |
| `interactive-hover` | 悬浮态表面 | 列表项/导航项 hover 背景 |
| `accent-primary` | 主信号色（青） | 当前激活/选中态、主操作按钮、链接 |
| `accent-secondary` | 次信号色（铜） | 告警/需要注意的强调、数据高亮标签 |
| `status-success` | 成功状态 | 已确认/已通过一类状态徽章 |
| `status-error` | 错误状态 | 错误边框/文字 |
| `status-error-strong` | 错误强调 | 确认删除一类高风险按钮 |
| `status-error-hover` | 错误态悬浮背景 | — |
| `text-on-accent` | 信号色块上的文字 | 深底浅字块反过来，配合 `bg-accent-*`/`bg-status-success` 使用 |
| `border-subtle` | 细边框颜色 | 例外——固定 alpha 通道，以完整 `rgba(...)` 值直接使用 |

三档皮肤的具体取值（通过 `<html data-skin="...">` 切换，机制见 §9）：

- **`dark`（信标本体，默认）**：深蓝黑基座（`#12161C`）+ 青色主信号（`#47B8D6`）+ 铜色次信号
  （`#F2A93C`）。新用户没有存过偏好时看到的就是这一档。
- **`default`（日间版本）**：浅蓝灰基座（`#EEF1F4` 一类），文字/边框反相，信号色相不变但加深
  到能在浅底上当前景色用（WCAG AA 校验：ink 14.45:1，ink-soft 6.47:1，accent-primary 前景色
  6.25:1，accent-secondary 前景色 5.22:1，全部通过正文 4.5:1 门槛）。
- **`business-blue`**：底色跟 `dark` 完全一致，只把主信号色从青偏移到靛蓝（WCAG 校验：
  `text-on-accent` 在这个靛蓝填充块上 4.96:1，通过）。

**取色原则**：`accent-primary`/`accent-secondary` 只用于真正的"信号"场景（激活/选中态、
告警强调），不用于纯装饰性的大面积填充（比如一条常驻可见的顶部横条）——信标方向的核心
主张是颜色只承担信号功能，常年占用一个信号色反而会让"这个颜色代表什么"变得含糊。纯装饰性的
大面积表面用 `bg-card`（呼应页面既有的深色系统），不要新发明色号。

## 3. 字体系统

两套字体，自托管为本地 `.woff2` 文件（不用 Google Fonts CDN，避免生产环境依赖外部字体服务
可用性），`@font-face` 声明 + 文件都在 `frontend/src/styles/index.css` / `frontend/public/fonts/`，
`frontend/index.html` 对关键字重做了 `<link rel="preload">`：

| Token | 字体栈 | 字重 | 用途 |
|---|---|---|---|
| `font-mono` | `"IBM Plex Mono", ui-monospace, "SFMono-Regular", monospace` | 400 / 500 / 600 | 标题/展示——**技术感的核心来源，标题也用等宽字体是信标方向刻意的选择**。页面级/区块级标题（`h1`/`h2`/`h3`、品牌名）统一 `font-mono font-semibold`（600，真实字重，不要用 `font-bold` 700——IBM Plex Mono 没有自托管 700 字重，会被浏览器合成粗体，观感发虚）。也用于全大写+字距拉宽的标签类文字。 |
| `font-sans`（默认） | `"IBM Plex Sans", system-ui, "PingFang SC", "Microsoft YaHei", sans-serif` | 400 / 500 | 全站默认正文。中文自动 fallback 到系统黑体。 |

**字重规则**：正文保持常规字重（400），强调文字（按钮标签、加粗提示）用 `font-medium`（500，
Sans 唯二自托管的字重之一）；标题用等宽字体 + `font-semibold`（600，见上表），不要在等宽标题
上用 `font-bold`。

## 4. 边框系统（无阴影）

信标方向不用阴影表达层次，一律用 1px 边框：`border border-subtle`（或语义色边框，如
`border-status-error`/`border-accent-secondary`）。没有 `shadow-*` 系列 Tailwind 扩展——
`tailwind.config.ts` 里没有 `boxShadow` 配置，任何 `shadow-soft`/`shadow-brutal` 之类的
class 都不会解析成任何样式，只会静默失效。

弹层的遮罩（modal/dialog 的背景蒙层）例外：不要用 `bg-ink/*`（`ink` 在深色皮肤下是浅色，
用作遮罩会让遮罩变亮而不是变暗，参见 `ConfirmContext.tsx`/`GraphReviewsPage.tsx` 的教训）。
遮罩应该在任何皮肤下都读作"变暗"，用固定的 `bg-black/40`，不走皮肤相关的 token。

## 5. 圆角规则

收紧的小圆角，`tailwind.config.ts` 的 `borderRadius` 扩展：

| Token | 值 | 用途 |
|---|---|---|
| `rounded-chip` / `rounded-control` / `rounded`（DEFAULT） | 2px | 标签、按钮、输入框 |
| `rounded-card` / `rounded-panel` | 3px | 卡片、面板 |
| `rounded-modal` / `rounded-container` | 4px | 弹层、大容器 |

不新增自定义圆角档位，按元素类型套用上表已有的 token。

## 6. 组件模式清单

以下模式已在 `frontend/src/` 落地，新增同类元素时直接复用对应 className 组合。

### 6.1 顶部公告条 / 品牌横条（`ChatPage.tsx`）
中性深色系统（`bg-card`），不用信号色做纯装饰性大面积填充（见 §2 取色原则）：
```
border-b border-subtle bg-card px-4 py-2 text-center font-mono text-xs uppercase tracking-widest text-ink-soft
```
品牌名用 `font-mono font-semibold text-ink`。

### 6.2 主按钮（Primary Button）
实心信号色 + 细边框，用于最主要的操作：
```
min-h-[44px] cursor-pointer rounded-control border border-subtle bg-accent-primary px-5 py-2.5 font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}
```
按下反馈是 `active:scale-95 active:opacity-90`（轻微缩放+变淡），不是旧设计语言的位移硬投影。
参考实现：`ChatInput.tsx` 发送按钮。

### 6.3 次级按钮（Secondary / Outline Button）
`bg-paper` 底 + 细边框，无信号色填充，用于非主线操作：
```
min-h-[44px] cursor-pointer rounded-control border border-subtle bg-paper px-3 py-1.5 text-sm font-bold text-ink transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}
```
**判断主/次的原则**：同一区域优先级更高的操作用主按钮样式，优先级低的用次级样式，不要两个
都用实心信号色（会分不清主次）。`min-h-[44px]`/`cursor-pointer`/`${focusRing}` 是所有按钮类
元素的强制要求，见第 7 节。

### 6.4 消息气泡（`MessageBubble.tsx`）
- 用户气泡：`border-subtle bg-accent-primary text-on-accent`
- 助手气泡（正常）：`border-subtle bg-card text-ink`
- 助手气泡（错误态）：`border-status-error bg-card text-ink`（**边框**标识错误，**文字**仍用
  `ink` 不用 `status-error`——低对比度红字达不到 WCAG AA 正文门槛，错误态靠边框颜色+文案
  内容传达语义，符合"不能只靠颜色传达信息"的可访问性原则）
- 公共部分：`max-w-[75%] rounded-card border px-4 py-3`（无阴影）

### 6.5 状态徽章 / 提示卡片（`TaskStatusBadge.tsx` 及各页面的通知条）
两种角色，不要混用：
- **小面积、离散的状态徽章**（如列表行内的状态标签）：实心信号色填充 + `text-on-accent` 是
  可以接受的（`TaskStatusBadge` 的 `active`/`warning` tone）。
- **大面积的提示卡片/通知条**（如"该租户 schema 尚未确认"这类横幅）：**不要**用信号色实心
  填充（会喧宾夺主，且如果卡片内还嵌了别的信号色徽章，会出现"一个节点两种信号色"的冲突）。
  统一用中性表面 + 语义色边框：`bg-card` + `border-status-error`（错误）或
  `border-accent-secondary`（警示/待办），文字用普通的 `text-ink`。参考：
  `DocumentsPage.tsx` 的失败任务卡片、`SchemaEtlPage.tsx` 的 schema 未确认提示。

### 6.6 引用标签 / 徽章（`SourceCitations.tsx`）
```
rounded-chip border border-subtle bg-accent-secondary px-2.5 py-1 text-xs text-on-accent
```

### 6.7 输入框
```
rounded-control border border-subtle bg-paper px-4 py-2.5 text-ink placeholder:text-ink-soft ${focusRing} disabled:opacity-50
```
focus 态用 `focusRing`（`focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink`，各文件内联定义或模块级常量，写法不统一但值统一），
不用 `shadow-*`（focus 态如果历史遗留写了 `focus:shadow-soft` 之类的 class，直接删除，不要
补边框——`${focusRing}` 已经承担了 focus 反馈，重复补偿是错的）。

### 6.8 Footer（`Footer.tsx`）
中性深色系统（`bg-card`），不是固定反色（同 §6.1 的取色原则）：
```
border-t border-subtle bg-card px-6 py-8 text-ink-soft
```
品牌名（不透明）用 `font-mono font-semibold text-ink`；分类小标题/说明文字用
`font-mono text-xs uppercase tracking-widest`。

### 6.9 管理后台侧边栏导航项 / 顶部 tab（`AdminLayout.tsx` 等）

未激活态：`border border-subtle px-3 py-2.5 text-sm font-bold bg-paper text-ink hover:bg-interactive-hover`；
激活态（当前路由/当前选中项）：`bg-accent-primary text-on-accent`（**统一用 `accent-primary`**，
不管这个元素历史上是不是用过别的强调色——"当前激活/选中"是同一个语义角色，应该在全站用同
一个信号色，不要因为某个文件历史上凑巧用了别的颜色就让它跟其它激活态长得不一样）。用
react-router 的 `NavLink` 的 `isActive` 判断，不手动比较路径字符串。

## 7. 交互规范

- **按下反馈**：所有按钮类元素用 `active:scale-95 active:opacity-90`（轻微缩放+变淡）。
- **禁用态**：统一 `disabled:opacity-50`，可点击元素额外加 `disabled:cursor-not-allowed`。
- **focus 态**：`${focusRing}` 统一处理（见 §6.7），不用浏览器默认 outline，也不用
  `shadow-*`（没有对应的 Tailwind 配置，会静默失效）。
- **触控目标**：所有可点击元素最小高度 `min-h-[44px]`，小尺寸按钮也不例外——用
  `text-sm`/`py-1.5` 做视觉上的"小"，但可点击区域仍要够 44px。
- **鼠标指针**：所有可点击元素加 `cursor-pointer`。
- **过渡**：涉及缩放/透明度变化的元素统一加 `transition`（不指定具体 duration/easing，用
  Tailwind 默认值）。
- **动效降级**：无限循环/自动播放的动画（如 `animate-bounce`）必须配
  `motion-reduce:animate-none` 或等效降级。
- **视口高度**：需要撑满视口高度的容器用 `min-h-dvh`，不用 `min-h-screen`。
- **弹层遮罩**：`bg-black/40`，不用皮肤相关 token（见 §4）。

## 8. 皮肤（skin）机制

`SkinContext.tsx`（`SkinProvider`）是站点级个人偏好（前台聊天页 + 后台管理共用，存
`localStorage`，key 为 `admin_skin`），通过 `<html data-skin="...">` 属性驱动 `index.css`
里对应的 `:root[data-skin='...']` 覆盖块生效。三档取值见 §2。没有存过偏好的新用户默认落在
`'dark'`（信标本体），不是 `'default'`——`default` 是给无法/不愿意用深色模式的用户的日间
版本，不该是大多数用户的第一眼观感。`SkinSwitcher` 组件挂载在 `AdminLayout.tsx` 里，前台
聊天页目前没有暴露皮肤切换入口（如果要加，逻辑上应该复用同一个 `useAdminSkin()`）。

## 9. 新增组件/页面时的检查清单

1. 颜色只用第 2 节表格里的语义 token，不写字面量色值，不新增装饰性强调色槽位。
2. 标题（`h1`/`h2`/`h3`、品牌名）用 `font-mono font-semibold`；正文默认继承 `font-sans`。
3. 卡片/按钮/输入框用第 5 节的圆角 token，不自造新档位。
4. 层次用边框（`border border-subtle` 或语义色边框），不用阴影——没有 `shadow-*` 配置。
5. 大面积表面（横条、footer）用中性 `bg-card`，信号色只用于真正的状态/激活场景（见 §2
   取色原则），提示卡片用"中性底+语义边框"而不是实心信号色填充（见 §6.5）。
6. 可点击元素必须有 `active:scale-95 active:opacity-90` 按下反馈 + `${focusRing}` + 见第 7 节。
7. 如果新模式在本文档里没有先例，先看设计规格
   `docs/superpowers/specs/2026-08-26-console-visual-identity-design.md` 里的设计基调判断，
   拿不准就参照本文档已有的组件模式类推，不要凭印象猜测。

## 10. 参考来源

- 设计规格：`docs/superpowers/specs/2026-08-26-console-visual-identity-design.md`
- 实施计划：`docs/superpowers/plans/2026-08-26-console-visual-identity.md`
- 本文档随代码演进持续更新，如与实际代码不一致，以代码为准并回来修正本文档。
