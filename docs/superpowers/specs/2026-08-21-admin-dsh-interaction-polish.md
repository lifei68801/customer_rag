# 后台交互打磨（参考 DSH）—— 设计决策记录

**日期**：2026-08-21
**背景**：基于对 DeepSeek Harness（DSH，本地 http://127.0.0.1:3080/）交互模式的调研（fork 产出 7 条建议），用户确认全部采纳。本文档记录 grill-me 访谈中敲定的具体设计决策，供 plan 执行时直接引用。

本次不涉及颜色/圆角/阴影等形状层面改动——项目的新粗野主义视觉语言（0 圆角、2px 实心黑边、`shadow-brutal`/`shadow-brutal-sm` 硬直角偏移阴影）保持不变，所有新组件必须复用这套语言，不得引入 DSH 原生的圆角/柔和阴影/细边框。

## 前提修正（核实后与 fork 原始建议不同的地方）

1. **"活跃中"脉冲**：代码里没有"排队中" vs "正在处理"的区分（`etl_runs` 只有 running/completed/failed，DocumentsPage 任务只有 stuck/正常两种）。脉冲动画只能是统一加在 `tone="active"` 上的"这个正在发生"信号，不做状态区分。
2. **悬停行图标暗示**：整个后台没有任何"行可展开/隐藏操作"的表格行——`OntologySchemaPage.tsx` 表格行的编辑/迁移/删除按钮本来就常驻显示，不是 hover 才出现。这条建议**跳过**，不做替代实现。
3. **Toast 触发范围**：现状里符合"确认性瞬时反馈、常驻不消失"描述的只有 `OntologySchemaPage.tsx` 里的 2 处 `migrateSuccessMessage`（实体类型/关系类型迁移成功）。删除、上传等操作现在完全没有任何成功反馈（不是"常驻文字"，是"压根没有"）。用户确认：范围扩大到所有增/删/改/审批操作。

## 决策 1：Toast 组件

- **视觉**：`bg-ink text-paper border-2 border-ink shadow-brutal`，深色实心块，与错误提示（`border-status-error`）在语气上区分开——toast 只用于成功确认，不用于错误。
- **位置**：`fixed top-4 left-1/2 -translate-x-1/2 z-50`，顶部居中，`pointer-events-none`（不挡点击）。
- **时序**：0.15s 淡入 → 停留 3s → 0.15s 淡出，共约 3.3s。
- **堆叠行为**：不排队。新 toast 直接覆盖旧的（重置计时器），不做多条堆叠。
- **交互**：无手动关闭按钮。
- **无障碍**：`role="status"` `aria-live="polite"`；遵守 `prefers-reduced-motion`（用 `motion-reduce:transition-none`，减弱动效时直接瞬间显示/消失，不做位移/缩放类动画）。
- **API**：`useToast(): (message: string) => void`，架构照抄 `ConfirmContext.tsx` 的 Provider + hook 模式。

### 触发范围（挂载点）

**架构前提**：`ConfirmProvider` 目前只挂在 `AdminLayout.tsx` 内部（管理员专属），但 `ChatSidebar.tsx`（前台聊天页的组件）也需要 `useConfirm()`（见"顺带修复"）和 `useToast()`。这与上一轮修复 `SkinProvider` 时遇到的问题同源：**`ConfirmProvider` 和新建的 `ToastProvider` 都必须挂到 `main.tsx` 的站点级根节点**，而不是只挂在 `AdminLayout` 下。`TenantProvider` 保留在 `AdminLayout.tsx`（租户确实是管理员专属概念，前台聊天页不需要）。

调用点清单（按文件）：

| 文件 | 函数 | 成功消息 |
|---|---|---|
| `OntologySchemaPage.tsx` | `handleConfirm`（实体类型确认为正式） | `已确认` |
| `OntologySchemaPage.tsx` | `handleDelete`（实体类型删除） | `已删除实体类型` |
| `OntologySchemaPage.tsx` | `handleMigrate`（实体类型迁移，**替换**原 `migrateSuccessMessage` 常驻文字） | `已迁移 {N} 条术语、{M} 个图谱节点`（沿用原文案） |
| `OntologySchemaPage.tsx` | `handleDelete`（关系类型删除） | `已删除关系类型` |
| `OntologySchemaPage.tsx` | `handleMigrate`（关系类型迁移，**替换**原 `migrateSuccessMessage`） | `已迁移 {N} 条边`（沿用原文案） |
| `OntologySchemaPage.tsx` | `handleAdd`（约束新增） | `已添加约束` |
| `OntologySchemaPage.tsx` | `handleRemove`（约束删除） | `已删除约束` |
| `DocumentsPage.tsx` | `handleUpload` | `已提交上传` |
| `DocumentsPage.tsx` | `handleDelete`（文档删除） | `已删除文档` |
| `DocumentsPage.tsx` | `handleRetryJob` | `已重新提交` |
| `DocumentsPage.tsx` | `handleDeleteJob` | `已删除任务` |
| `GraphReviewsPage.tsx` | `handleApprove` | `已批准` |
| `GraphReviewsPage.tsx` | `handleReject` | `已驳回` |
| `GraphReviewsPage.tsx` | `handleBatchApprove` | `已批准 {N} 条` |
| `GraphReviewsPage.tsx` | `handleBatchReject` | `已驳回 {N} 条` |
| `GraphReviewsPage.tsx` | `handleSubmitCreateEntity` | `已创建实体候选` |
| `TermsPage.tsx` | `handleSaveEdit` | `已保存` |
| `TermsPage.tsx` | `handleDelete` | `已删除实体` |
| `SchemaEtlPage.tsx` | `handleUpload`（提交运行） | `已提交运行` |
| `ChatSidebar.tsx` | `handleDelete`（删除会话） | `已删除会话` |

**不加 toast 的操作**：纯 UI 展开/收起（`handleTogglePreview`、`handleStartEdit`/`handleCancelEdit`）、只读下载（`handleDownloadFile`/`handleDownloadReport`/`handleDownloadSample`）——这些操作本身的界面变化已经是反馈，不需要额外确认。所有 `role="alert"` 错误提示保持原样不动（阻断性错误不挪去 toast）。

## 决策 2：空状态改造（7 处 + 1 处顺带发现）

按是否存在"当前页面可执行的动作"分 3 组：

**组 A：真链接**（跳转到另一个能创建内容的页面）
- `DocumentsPage.tsx:528` → 改成 `<Link to="/admin/data-entry">`，文案引导去"数据加工"
- `TermsPage.tsx:319` → 原文案里的「表格导入」「文档抽取」改成真正的 `<Link>`，分别指向 `/admin/data-entry/etl` 和 `/admin/data-entry/review`

**组 B：同页提示**（动作就在本页面的其它位置，不新增按钮，只在文案里点出方位）
- `OntologySchemaPage.tsx:552`（实体类型，`view==='draft'` 时）→ 提示"点击上方「+ 新增实体类型」创建一个"
- `OntologySchemaPage.tsx:930`（关系类型，`view==='draft'` 时）→ 提示"点击上方「+ 新增关系类型」创建一个"
- `OntologySchemaPage.tsx:1258`（约束，`view==='draft'` 时）→ 添加约束的表单在列表**下方**（不是上方），提示"在下方表单添加一个"
- `SchemaEtlPage.tsx:462` → 提示"在上方上传数据文件开始第一次运行"
- `ChatSidebar.tsx:76`（顺带归入本组，见下方说明）→ 提示"点击上方「+ 新建会话」开始"

**组 C：只改排版，不加 CTA**（真正的日志/历史类空状态，本页无法创建）
- `OntologySchemaPage.tsx:552/930/1258`（`view==='confirmed'` 时，与组 B 是同一处代码但按 view 分支文案）→ 只是把句子写得更自然，不提任何按钮
- `GraphReviewsPage.tsx:856`（审核历史）→ 只改文案，说明这里以后会出现什么（"批准或驳回过的候选会出现在这里"），不加按钮

## 决策 3：加载态改成骨架屏

- **视觉**：不用 DSH 的圆形 spinner（与直角/实边冲突）。改成方块占位 + `animate-pulse`（Tailwind 内置的透明度脉冲工具类，1↔0.5，2s 循环，不需要自定义 keyframe）。
- **颜色**：占位块用 `bg-ink-soft/40`（半透明，三套皮肤下都可见）。
- **组件**：新建 `Skeleton.tsx`，两种 `variant`：
  - `table-rows`：仿表格行，`border-2 border-ink bg-card shadow-brutal-sm` 外框 + `count` 行、每行 2-3 个不同宽度的占位条（模拟列宽差异），行与行之间 `border-b border-ink/20`
  - `card-list`：仿卡片列表项，`count` 个 `border-2 border-ink bg-card p-4 shadow-brutal-sm` 卡片，每卡片内 2 条不同宽度占位条（标题宽 + 副标题宽）

### 替换点

| 文件:行 | 现状 | 替换成 |
|---|---|---|
| `OntologySchemaPage.tsx:550` | `{!loaded && <p>加载中…</p>}` | `<Skeleton variant="table-rows" count={4} />` |
| `OntologySchemaPage.tsx:929` | 同上 | 同上 |
| `OntologySchemaPage.tsx:1256` | 同上 | 同上 |
| `TermsPage.tsx:204` | 同上 | `<Skeleton variant="table-rows" count={5} />` |
| `DocumentsPage.tsx:451` | 同上 | `<Skeleton variant="card-list" count={3} />` |
| `GraphReviewsPage.tsx:614` | 同上（pending tab） | `<Skeleton variant="card-list" count={3} />` |
| `GraphReviewsPage.tsx:826` | 同上（history tab） | `<Skeleton variant="card-list" count={3} />` |

`SchemaEtlPage.tsx` 的"历史跑批"没有独立的加载态代码（直接渲染 `runs`，不判断 `!loaded`），不在本次改造范围内。

## 决策 4："活跃中"脉冲

`TaskStatusBadge.tsx` 的 `tone==='active'` 语气在文字前加一个 `0.5rem` 小正方形色块，`opacity` 在 1↔0.3 之间用 `animate-pulse`（或等效自定义动画，周期 1.4s）循环闪烁；律钱本身的字体/边框/背景色不变。其余 4 种语气（neutral/success/error/warning）不受影响。遵守 `prefers-reduced-motion`（`motion-reduce:animate-none`）。

## 决策 5：列表密度切换

- **架构**：新建 `DensityContext.tsx` + `DensitySwitcher.tsx`，完全照抄 `SkinContext.tsx`/`SkinSwitcher.tsx` 的模式：
  - `DensityId = 'standard' | 'compact'`
  - `localStorage` key：`admin_density`
  - `useEffect` 同步到 `document.documentElement.setAttribute('data-density', density)`（做法上跟皮肤一致，但**密度不走 CSS 变量**——间距是 Tailwind 静态类名，不是像颜色那样能用 `var()` 驱动，所以各消费组件要自己读 `useAdminDensity()` 后二选一 className，`data-density` 属性只作为可选的 CSS hook 保留，不强制使用）
  - `DensitySwitcher` 挂载位置：`AdminLayout.tsx` 侧边栏，紧挨着 `SkinSwitcher`
- **作用范围**（只用在会随数据增长变长的列表/表格，共 6 处）：
  - `OntologySchemaPage.tsx` 的实体类型表格、关系类型表格、约束表格（3 处）
  - `TermsPage.tsx` 实体列表
  - `DocumentsPage.tsx` 文档列表
  - `GraphReviewsPage.tsx` 审核列表（pending + history 共用同一套间距规则）
- **具体数值**：
  - 表格单元格：标准 `px-3 py-2` → 紧凑 `px-2 py-1`
  - 卡片列表项：标准 `p-4` → 紧凑 `p-2.5`
- **不纳入范围**：`ChatSidebar.tsx` 会话列表（容器本身就矮，`max-h-64`/`max-h-none`）、`SchemaEtlPage.tsx` 跑批记录（本次决策未涵盖，维持标准间距）。

## 决策 6：Tooltip 组件

- **适用范围**：全项目只有 2 处真正的纯图标控件——`ChatSidebar.tsx` 删除会话的垃圾桶图标按钮、`Pager.tsx` 的 `‹`/`›` 上一页/下一页按钮。
- **定位**：固定在控件正上方展开，不做防溢出智能定位（这 2 处控件位置固定，不存在被视口边缘遮挡的情况）。
- **视觉**：`border-2 border-ink bg-ink text-paper shadow-brutal-sm px-2 py-1 text-xs font-bold`，0.15s 延迟后 0.15s 淡入。
- **触发**：`onMouseEnter`/`onMouseLeave` + `onFocus`/`onBlur`（键盘可达）。
- **API**：`<Tooltip label="删除会话"><button>...</button></Tooltip>`，包裹子元素，不改变子元素自身的 `aria-label`（两者并存：`aria-label` 给屏幕阅读器，Tooltip 给视觉用户）。

## 决策 7：顺带修复 —— ChatSidebar.tsx 残留的 `window.confirm()`

`components/ChatSidebar.tsx:47` 的 `handleDelete` 仍在用原生 `window.confirm()`（早期把所有 `window.confirm` 改成 `ConfirmContext` 时漏改的一处）。本次一并改成 `await confirm(...)`，与项目其它地方保持一致。前提：`ConfirmProvider` 提升到站点级（见决策 1 的"触发范围"部分），`ChatSidebar.tsx` 所在的 `ChatPage` 才能拿到 `useConfirm()`。

## 明确不做的事

- 不做 DSH 的圆角/柔和阴影/细边框视觉语言迁移（与新粗野主义冲突，此前已明确判定）。
- 不做悬停行图标暗示（无真实目标，见"前提修正"）。
- 不做"排队中" vs "正在处理"的状态区分（数据模型不支持）。
- Toast 不支持多条堆叠、不支持手动关闭。
- Tooltip 不做防溢出智能定位。
