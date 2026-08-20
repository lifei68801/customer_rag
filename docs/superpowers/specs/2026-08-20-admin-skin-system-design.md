# 管理后台皮肤（配色主题）系统 设计方案

日期：2026-08-20

## 背景

用户希望参考 DeepSeek Harness（DSH）插件生态里"皮肤中心"的机制（皮肤与插件解耦、CSS 变量动态注入、无刷新切换），给当前项目的管理后台也做一套可切换的配色皮肤。

DSH 的原始机制面向的是一个桌面端 AI 编程工具的插件生态：皮肤是独立分发的资产目录（`skin.json` 清单 + 样式 + 贴图/壁纸 + 可选特效脚本），支持"试穿再应用"、甚至能接入 Wallpaper Engine 做动态壁纸背景。这些能力是为桌面端沉浸式界面设计的，对本项目这样一个 B2B 客服 RAG 系统的后台管理界面是明显过度设计——本方案只借鉴其"CSS 变量驱动、运行时无刷新切换"这个核心思路，不照搬资产目录/清单文件/试穿预览/壁纸这些机制。

## 决策记录（本次 grill-me 访谈定下）

1. **范围：只做配色 + 阴影颜色**，不动字体、不动布局、不动组件结构。当前"neo-brutalist"设计语言（粗边框、硬阴影）本身是统一的视觉身份，皮肤只换色调，不改变这套视觉语言的结构性特征。
2. **受众：个人偏好，不是租户品牌化**。每个管理员自己选自己的皮肤，存浏览器本地（`localStorage`），跟 `tenant_id` 无关，不需要后端参与、不需要新增数据库表或 API。
3. **皮肤形式：固定预设列表，不做自定义取色器**。三套皮肤直接编译进代码，用户从下拉框选一个，不提供颜色选择 UI。
4. **三套预设皮肤**，只替换 `paper`/`ink`/`ink.soft`/`card` 四个 token，其余全部不变：
   - **默认**（`default`）：维持现状不变。
   - **暗色**（`dark`）：直接采用从本地运行的 DSH 实例（`127.0.0.1:3080`）抓取到的真实官方深色值（见"配色取值来源"一节），不是臆造的近似值。
   - **商务蓝**（`business-blue`）：自行设计的冷灰蓝配色，给不喜欢默认撞色风格、想要更沉稳观感的用户。
5. **`accent`（pink/yellow/cyan/green/orange）和 `status`（success/error）三套皮肤共用同一组色值，不随皮肤变化**——这些高饱和强调色本身在三套背景色下都有足够对比度，且能让新增一套皮肤的成本降到最低（只需定义 4 个颜色值）。

   **更新（2026-08-20，实现完成后）**：这条决策被推翻——用户在实际用起来后要求 `accent`/`status` 也要随皮肤变化。用完全相同的 CSS 变量机制扩展（`--color-accent-pink` 等 7 个新变量，三个 `:root[data-skin]` 块各自定义一套取值），不是新的技术方案。具体取值：默认皮肤原样不变；暗色皮肤统一提亮/提高饱和度（在近黑背景上更"跳"）；商务蓝皮肤统一降饱和度、往灰调偏（呼应它"沉稳专业"的定位）。见 `frontend/src/styles/index.css` 里三个 `:root[data-skin]` 块的最终取值，这里不重复列出以免和代码脱节。
6. **技术方案**：把 `tailwind.config.ts` 里 `paper`/`ink`/`ink.soft`/`card` 四个 token 从写死的 hex 改成引用 CSS 自定义属性（如 `ink: 'var(--color-ink)'`），在全局样式表里用 `:root` 定义默认值、`:root[data-skin="dark"]`/`:root[data-skin="business-blue"]` 覆盖对应取值；切换皮肤 = 给 `<html>` 元素的 `data-skin` 属性赋新值。这是唯一能做到"运行时无刷新切换、不需要重新构建"的方式，跟 DSH"CSS 变量动态注入"同一个思路，但不需要它那套"资产目录+清单文件动态加载"的复杂加载器——皮肤是固定的几套、直接编译进代码。
7. **阴影颜色也要变量化**：`boxShadow.brutal`/`brutal-sm` 目前写死引用 `#141111`（即 ink 色），改成 `var(--color-ink)`，让阴影颜色跟着 ink 自动换。
8. **UI 位置**：侧边栏底部，跟"返回前台"/"登出"按钮放一起（`AdminLayout.tsx` 已有区域），做成一个下拉选择框，视觉风格照抄已有的 `TenantSwitcher.tsx`（同样是 `<label>` + `<select>`，边框/阴影/焦点样式完全一致）。
9. **持久化**：`localStorage`（不是 `TenantContext` 用的 `sessionStorage`——皮肤偏好应该跨浏览器会话保留，跟"当前操作哪个租户"这种会话级状态语义不同）。
10. **不处理 FOUC（刷新瞬间闪一下默认皮肤）**：不在 `index.html` 里加内联脚本提前设置 `data-skin`。这是内部管理工具，偶尔的轻微闪烁代价很小，换来的是不需要脱离 React 组件体系维护一段手写内联 JS。

## 配色取值来源

**默认皮肤**：`frontend/tailwind.config.ts` 现有值，原样保留：
- `paper`: `#FFFAEF`
- `ink`（DEFAULT）: `#141111`
- `ink.soft`: `#5C5750`
- `card`: `#FFFFFF`

**暗色皮肤**：从本地运行的 DSH 实例（`http://127.0.0.1:3080/`）实际抓取验证——下载其 `/assets/index-*.css`，在其中找到的真实 CSS 自定义属性（DSH 启动界面用的浅色/深色配对变量）：
- `--dsh-boot-bg`: 浅 `#ffffff` / 深 `#151517`
- `--dsh-boot-brand` / `label-primary`: 浅 `#0f1115` / 深 `#f9fafb`
- `label-secondary`: 浅 `#61666b` / 深 `#cfd3d6`
- `--dsw-hovercard-bg`: `#2C2C2E`（DSH 深色模式下的卡片/悬浮层背景，唯一取值，无浅色对应版本）

映射到本项目的 token（取 DSH 的深色列）：
- `paper`: `#151517`
- `ink`（DEFAULT）: `#f9fafb`
- `ink.soft`: `#cfd3d6`
- `card`: `#2C2C2E`

注：DSH 本身的视觉识别是纯黑白灰的极简中性色调，没有类似本项目"粉/黄/青/绿/橙"这种高饱和撞色强调色——这次抓取到的完整 CSS 里，除了上述灰阶值，其余彩色值全部是 JSON 语法高亮用的颜色（`#1c00cf`/`#c41a16`/`#881391` 等，是 Chrome DevTools 经典 JSON 查看器配色，跟 UI 主题无关），不纳入本方案。

**商务蓝皮肤**：自行设计，无外部数据来源：
- `paper`: `#F4F6F8`
- `ink`（DEFAULT）: `#1B2430`
- `ink.soft`: `#5A6B7B`
- `card`: `#FFFFFF`

**默认皮肤下的 `accent`/`status`**（其余两套皮肤的取值见下方"更新"）：
- `accent.pink`: `#FE7DA8`
- `accent.yellow`: `#FFD440`
- `accent.cyan`: `#27CCF3`
- `accent.green`: `#A9D877`
- `accent.orange`: `#F8A16F`
- `status.success`: `#A9D877`
- `status.error`: `#DC2626`

**更新（2026-08-20，实现完成后）**：原计划这两组"三套皮肤共用、不随皮肤变化"，后来被推翻——见上方"决策记录"第 5 条的更新说明。暗色皮肤：`accent.pink #FF8FC0` / `accent.yellow #FFDD5C` / `accent.cyan #4DD8FF` / `accent.green #B8E68C` / `accent.orange #FFAD7D` / `status.success #B8E68C` / `status.error #EF4444`。商务蓝皮肤：`accent.pink #C97B94` / `accent.yellow #D4A94A` / `accent.cyan #5A9BB8` / `accent.green #7FA688` / `accent.orange #C98A5D` / `status.success #7FA688` / `status.error #C0392B`。

## 作用范围（未在访谈里单独讨论，这里作为实现细节直接确定，不构成新的开放决策）

`SkinProvider` 只包裹 `AdminLayout` 内部（跟 `TenantProvider` 现在的包裹范围一致），不延伸到 `App.tsx` 顶层的 `/`（`ChatPage`，面向客户的产品前台，不在本次讨论范围内）或 `/admin/login`（登录页，皮肤偏好在登录之后的管理后台里才有意义）。

## 涉及的现有文件（本方案的实现基础）

- `frontend/tailwind.config.ts`：颜色 token 定义，`paper`/`ink`/`card` 四项改为 CSS 变量引用，`boxShadow` 两项改为引用 `var(--color-ink)`。
- `frontend/src/styles/index.css`：新增 `:root` 默认变量定义 + 两个 `[data-skin="..."]` 覆盖块。
- `frontend/src/admin/TenantContext.tsx`：新 `SkinContext.tsx` 的直接参照模板（Context + Provider + hook 的结构）。
- `frontend/src/admin/TenantSwitcher.tsx`：新 `SkinSwitcher.tsx` 的直接参照模板（`<label>` + `<select>` 的视觉/交互模式）。
- `frontend/src/admin/AdminLayout.tsx`：新增 `SkinProvider` 包裹 + 在侧边栏底部渲染 `SkinSwitcher`。
