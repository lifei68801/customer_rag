# 视觉语言转向：从新粗野主义到 DSH 风格圆角柔和阴影 —— 设计决策记录

**日期**：2026-08-21
**背景**：用户推翻了本会话早前"DSH 的圆角/柔和阴影/细边框跟本项目新粗野主义互斥，不采用"的判断，决定真正转向 DSH 风格。这是一次改变整个项目视觉身份的改动，影响前台聊天页 + 后台管理的全部页面。

## 现状盘点（改动前）

- `shadow-brutal`：`2px 2px 0 0 var(--color-ink)`，硬直角偏移阴影，无模糊 —— 138 处引用
- `shadow-brutal-sm`：`1px 1px 0 0 var(--color-ink)` —— 81 处引用
- `border-2 border-ink`：2px 实边黑框 —— 约 150 处（绝大多数），另有约 15 处 `border-2 border-status-error`（错误态强调边框）
- 圆角：项目里完全没有系统性圆角，只有 5 处历史遗留的孤立 `rounded`/`rounded-full`（`MarkdownContent.tsx` 的行内代码块、`MessageBubble.tsx` 的打字指示器圆点）
- 点击反馈：45+ 处按钮用 `active:translate-x-px active:translate-y-px active:shadow-none`（部分是 `translate-x-[2px]`），按下时位移+阴影消失，模拟物理按压

## 决策 1：圆角 —— 完整照搬 DSH 的分级体系

DSH 真实圆角值：6px/7px/8px/12px/14px/18px/24px/50%。按语义角色而非数值大小分组给出这 8 组档位，加进 `tailwind.config.ts` 的 `borderRadius`（自定义 key，不是覆盖 Tailwind 默认的 sm/md/lg，因为那套是"数值大小"语义，这里要的是"元素角色"语义，跟项目已有的 `accent.pink`/`status.error` 这类按用途命名的 token 风格保持一致）：

```ts
borderRadius: {
  none: '0',
  chip: '6px',       // 徽章/标签/小状态指示：TaskStatusBadge、来源引用标签、"来源：xxx"标签
  control: '7px',    // 按钮、输入框、下拉选择器
  DEFAULT: '8px',    // 兜底默认（大多数 <button>/<input> 走 control，DEFAULT 留给没有更具体分类的元素）
  card: '12px',      // 列表项卡片、表格外框
  panel: '14px',     // 表单区块、大面板、toast
  modal: '18px',     // 确认弹窗、下拉菜单、tooltip
  container: '24px', // 页面级大容器（聊天窗口整体外框这类，用得少）
  full: '9999px',    // 头像/圆点/圆形图标按钮——Tailwind 默认值已是这个，不用改
}
```

## 决策 2：阴影 —— 完全改成柔和多层阴影

放弃硬直角偏移阴影，改成标准的多层模糊阴影（业界常见的 elevation 阴影配方）。**保留 `shadow-brutal`/`shadow-brutal-sm` 这两个 class 名字会产生误导**（"brutal"意味着硬朗，但值已经变柔和），改名为 `shadow-soft`/`shadow-soft-sm`，随同一次全局替换一起做（反正要过一遍全文件 sed，改名字几乎不增加成本）：

```ts
boxShadow: {
  soft: '0 4px 6px -1px var(--shadow-color-1), 0 2px 4px -2px var(--shadow-color-2)',
  'soft-sm': '0 1px 3px 0 var(--shadow-color-1), 0 1px 2px -1px var(--shadow-color-2)',
}
```

`--shadow-color-1`/`--shadow-color-2` 是新增的 CSS 变量（每套皮肤各一份，见决策 4），分别对应"外层大范围淡阴影"和"内层小范围深阴影"两层。

### 点击反馈：位移+阴影消失 → 缩放+透明度

45+ 处 `active:translate-x-px active:translate-y-px active:shadow-none`（含 `-[2px]` 变体）统一替换成 `active:scale-95 active:opacity-90`。这个替换要跟对应元素补上 `transition-transform`（大部分已有 `transition` 类，`transition` 默认只包含 `transform, opacity` 等常见属性，Tailwind 的 `transition` 工具类本身就包含 transform，不需要额外加 `transition-transform`，可以确认后再定）。

## 决策 3：边框 —— 从 2px 实边改 1px 半透明细边

- `border-2 border-ink` → `border border-subtle`（`border-subtle` 是新的语义颜色 token，指向 3 套皮肤各自的低透明度 ink 色，见决策 4）
- `border-2 border-status-error`（约 15 处，错误态强调边框）**不降级成 subtle**——错误提示需要保留视觉分量，改成 `border border-status-error`（宽度统一降到 1px 跟整体变轻的语言一致，但颜色保持满饱和度的 status-error，不淡化）
- 已有的零星 `border border-ink/40`、`border border-ink-soft`、`border border-status-success` 等历史细边框保持不动（本来就是 1px，不在这次改造范围内）

**技术实现细节**（不是设计决策，是踩过坑后的实现约束）：本会话早前发现 Tailwind 对本项目这种"值是 `var(--color-x)` 的自定义颜色"不支持 `/opacity` 修饰符（`bg-status-error/20` 曾经编译失败），所以 `border-subtle` 不能写成 `border-ink/10` 这种形式，必须是一个独立的、每套皮肤预先算好透明度值的新 CSS 变量 `--color-border-subtle`（写成 `rgba(...)` 直接带 alpha 通道，不依赖 Tailwind 的透明度修饰符语法）。

## 决策 4：CSS 变量新增（`frontend/src/styles/index.css`，三套皮肤各一份）

| 变量 | 默认皮肤 | 暗色皮肤 | 商务蓝皮肤 |
|---|---|---|---|
| `--color-border-subtle` | `rgba(20, 17, 17, 0.12)` | `rgba(249, 250, 251, 0.14)` | `rgba(27, 36, 48, 0.12)` |
| `--shadow-color-1`（外层淡） | `rgba(20, 17, 17, 0.08)` | `rgba(0, 0, 0, 0.35)` | `rgba(27, 36, 48, 0.08)` |
| `--shadow-color-2`（内层深） | `rgba(20, 17, 17, 0.14)` | `rgba(0, 0, 0, 0.5)` | `rgba(27, 36, 48, 0.14)` |

暗色皮肤的阴影透明度明显更高（0.35/0.5 vs 0.08/0.14）——纯黑背景下浅色阴影几乎不可见，暗色模式下阴影本来就需要更深、更不透明才能被感知到，这是暗色 UI 设计的常规做法，不是三套皮肤形状语言不统一（形状语言——圆角值、阴影层数、边框宽度——三套皮肤完全一致，只有颜色深浅按各自背景做了必要适配）。

## 决策 5：三套皮肤形状语言统一

圆角、阴影层数/结构、边框宽度三者对默认/暗色/商务蓝完全一致，只有决策 4 里列出的颜色深浅按皮肤自身背景做适配。不做任何形状层面的差异化。

## 决策 6：改动范围 —— 前台 + 后台一起改

`shadow-soft`/`border-subtle` 这些是全局 CSS 变量/token，后台管理和前台聊天页共用同一份 `tailwind.config.ts`/`index.css`，天然会一起变。这次会明确地给两边的每个组件都补上合适的圆角档位（前台此前完全没有圆角，需要新增）。

## 决策 7：布局微调 —— 仅 1 处

重新调研 DSH 的布局数值（padding/gap/字号/行高）后发现：DSH 的"外壳级"布局数值（侧边栏宽度、响应式断点）是运行时 JS 算出来的，静态编译产物里提取不到，不能瞎编；而当前项目表单/表格间距（`gap-3`/`gap-1`/`px-3 py-2` 这类）本来就落在 DSH 8-14px 的紧凑留白区间里，没有明显差距。唯一站得住脚的改动：

- `frontend/src/pages/ChatPage.tsx` 聊天消息列容器 `max-w-3xl`（768px）→ `max-w-4xl`（896px）——长回答/代码块/引用来源列表在宽屏下会更舒展。这条是产品判断，不是 DSH 数据支撑。

## 圆角档位 → 组件角色映射（供 plan 逐文件套用）

这不是要求逐一确认的设计决策，是技术团队按上面 8 档语义分类实际执行时的映射表：

- **`rounded-chip`（6px）**：`TaskStatusBadge.tsx` 的徽章、`SourceCitations.tsx` 的来源引用标签、`TermsPage.tsx` 里的"来源：xxx"小标签、`GraphReviewsPage.tsx` 的"新建"小徽章
- **`rounded-control`（7px）**：所有 `<button>`（含 `active:scale-95` 那批）、`<input>`/`<select>`/`<textarea>`、`CopyButton.tsx`、`Pager.tsx` 的页码按钮
- **`rounded-card`（12px）**：`DocumentsPage.tsx`/`GraphReviewsPage.tsx` 的列表卡片、`OntologySchemaPage.tsx`/`TermsPage.tsx` 的表格外框（`overflow-x-auto border ...` 那层容器）、`Skeleton.tsx` 的两种占位容器、`MessageBubble.tsx` 的消息气泡
- **`rounded-panel`（14px）**：`OntologySchemaPage.tsx` 里的新增/编辑表单区块（`shadow-soft` 那层）、`SchemaEtlPage.tsx` 的上传表单区块、`ToastContext.tsx` 的 toast 提示条
- **`rounded-modal`（18px）**：`ConfirmContext.tsx` 的确认弹窗、`Tooltip.tsx` 的提示条（tooltip 虽小但语义上是浮层，不是 control）
- **`rounded-container`（24px）**：`ChatWindow.tsx` 最外层容器（如果有整体外框的话，需要执行时读代码确认）
- **`rounded-full`**：`MessageBubble.tsx` 的打字指示器圆点（已有，不用动）、如果有圆形图标按钮（本项目目前没有严格意义上的圆形图标按钮，执行时确认）

## 明确不做的事

- 不做圆角/阴影/边框以外的形状改动（比如不引入 DSH 的字号/行高体系，那是排版层面，这次不动）
- 不做三套皮肤的形状差异化
- 不改动上一轮刚做完的列表密度（standard/compact）功能本身，只是密度模式下的卡片/表格外框现在会带圆角
- 布局层面只改 1 处（ChatPage 消息列宽度），不做侧边栏宽度、断点等无数据支撑的改动
