# 全站视觉重设计：信标（Console）方向设计规格

## 背景与决策过程

用户通过 `/frontend-ui-engineering` 发起"重新设计UI和UX"的请求，范围未指定。探索现有前端后发现：项目已经有一套成熟的设计系统（`frontend/src/styles/index.css` + `tailwind.config.ts`）——RGB 三元组 CSS 变量供 Tailwind 消费（支持透明度修饰符）、三套可切换 skin（`default`/`dark`/`business-blue`）、密度切换、44px 最小触点、焦点环、响应式侧边栏折叠。这不是通用 AI 审美，而是已落地的自定义"新粗野主义"语言（粗边框、强对比色块、按压反馈）。

因此在动手前先跟用户确认了三个问题，用户依次选择：

1. **改造范围**：前台客服聊天 + 后台管理，两者都要。
2. **对现有设计语言的处理**：不在现有语言上打磨，换一套完全不同的视觉方向。
3. **驱动原因**：没有具体痛点，整体提升。

由于"换一套完全不同的方向"是主观审美判断，不该由我单方面决定，做了三个具体方向的对比提案（发布为 artifact，配色/字体/圆角/间距各自独立，铺在同样的真实产品内容上：客服问工单 + 术语合并审核）：

- **静航 Harbor**——浅色、冷静、克制，适合信息密度高的管理后台，但前台记忆点弱。
- **炉火 Ember**——深色暖调，客服前台亲和力强，但后台批量操作时视觉偏松散。
- **信标 Console**——深色技术感，配得上 ETL/图谱这类"数据管线"后台，但对普通客户可能偏硬。

用户选择**信标**，并在确认"后台技术感强、前台可能偏硬"这个已标注的风险后，明确要求**前后台统一用信标**，不做前台单独软化。

## 设计基调

"信标"的核心主张：这套系统在处理真实的数据管线（ETL 配置、知识图谱、相似度打分、多租户术语库），颜色只承担"信号"功能——用来标状态、标强调，不用来营造情绪。深色基座、克制的双色信号（青色为主信号，铜色为告警/次强调），等宽字体贯穿标题与数据展示，营造"精密仪表盘"而不是"聊天软件"的气质。这个基调贯穿前台聊天页与后台全部管理页面，不做区分。

## 色彩 Token

延用现有基础设施（`--color-*` 变量必须是空格分隔 RGB 三元组，`--color-border-subtle`/`--shadow-color-*` 例外，保持 rgba 直写）。以下是新的语义值：

| 变量 | 值（RGB 三元组） | 说明 |
|---|---|---|
| `--color-paper` | `18 22 28` (#12161C) | 主背景，蓝黑色基座 |
| `--color-ink` | `227 232 237` (#E3E8ED) | 主文字 |
| `--color-ink-soft` | `137 150 163` (#8996A3) | 次要文字 |
| `--color-card` | `24 30 38` (#181E26) | 卡片/面板表面 |
| `--color-interactive-hover` | `32 40 50` (#202832) | 悬浮态表面 |
| `--color-accent-primary` | `71 184 214` (#47B8D6) | 主信号色（青），导航激活态、主按钮、链接 |
| `--color-accent-secondary` | `242 169 60` (#F2A93C) | 次信号色（铜），告警/需要注意的强调、数据高亮 |
| `--color-status-success` | `79 190 138` (#4FBE8A) | 成功状态 |
| `--color-status-error` | `226 88 74` (#E2584A) | 错误状态 |
| `--color-status-error-strong` | `196 58 43` | 错误强调（如确认删除按钮） |
| `--color-status-error-hover` | `58 30 26` | 错误态悬浮背景 |
| `--color-text-on-accent` | `12 16 21` (#0C1015) | 信号色块上的文字（深底浅字块反过来） |
| `--color-border-subtle` | `rgba(227, 232, 237, 0.14)` | 保持 rgba 直写 |
| `--shadow-color-1` / `--shadow-color-2` | 见下方"阴影"节 | |

### Tailwind 层的破坏性变更

现有 `tailwind.config.ts` 的 `accent.{pink,yellow,cyan,green,orange}` 五个装饰性强调色槽，要收窄成两个语义化信号色槽：`accent.primary`（青）、`accent.secondary`（铜）。现状用量摸底（跑在 `frontend/src/` 下）：

- `accent-pink`：30 处——事实上的"主强调"槽位（导航激活态、主按钮等），全部改成 `accent-primary`。
- `accent-yellow`：7 处——次强调，改成 `accent-secondary`（少数纯装饰用途视具体页面改成中性色）。
- `accent-green`：2 处——跟 `status-success` 语义重叠，改用 `status-success`，删除这个槽位。
- `accent-cyan`：1 处——名字凑巧撞上新主色，逐一确认后改成 `accent-primary`。
- `accent-orange`：0 处，直接删除，无消费方。

这是一次**跨文件破坏性 token 重命名**，`rounded-{chip,control,card,panel,modal,container}` 161 处引用、`shadow-soft{,-sm}` 143 处引用也都要跟着圆角/阴影的新数值走（见下）——这就是为什么这份设计规格之后要走 `writing-plans` + `subagent-driven-development`，而不是直接改：不能让 token 改名和消费方改造出现中间断档状态。

## 字体

替换现有 `Space Grotesk` / `Space Mono`（这两个是 AI 生成界面的"安全默认"选择，且信标方向本身就要求更强的等宽/技术气质）：

- **标题/展示**：`IBM Plex Mono`（500/600 字重）——技术感的核心来源，标题也用等宽字体是信标方向刻意的选择。
- **正文/UI**：`IBM Plex Sans`（400/500 字重）。
- 中文回退栈不变：`"PingFang SC", "Microsoft YaHei", sans-serif`。
- Google Fonts 引入：`https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap`（前端非 artifact 环境，走正常的 `<link>` 或本地托管字体文件，两种方式都要在实施计划里明确选一个——推荐本地托管以避免生产环境依赖外部字体 CDN 的可用性风险，这点留给实施计划决定）。

## 圆角与阴影

信标方向的技术感要求整体收紧圆角、去掉大部分阴影，改用 1px 边框做层次：

| Token | 现有值 | 新值 |
|---|---|---|
| `rounded-chip` | 6px | 2px |
| `rounded-control` | 7px | 2px |
| `rounded` (DEFAULT) | 8px | 2px |
| `rounded-card` | 12px | 3px |
| `rounded-panel` | 14px | 3px |
| `rounded-modal` | 18px | 4px |
| `rounded-container` | 24px | 4px |
| `shadow-soft` | 双层柔和阴影 | 移除，改用 `border border-subtle`（现有 143 处 `shadow-soft*` 用法要逐一改成边框或直接删除，具体按场景在实施计划里判断） |

## 皮肤（skin）机制的处理

现有 `SkinContext`/`SkinSwitcher` 是站点级个人偏好（前后台共用，存 localStorage），提供 `default`/`dark`/`business-blue` 三档切换，是已经上线的用户可见功能，不属于"待重新设计的视觉表现"本身，而是一个功能开关——这次改造**不主动移除它**，但三档的具体取值要在信标语言内部重新定义，不能有一档还停留在旧的粗野主义配色：

- `dark`（信标默认基调）→ 上表的深蓝黑基座 + 青色信号，即"信标"本体。
- `default`（浅色项，供无法/不愿意用深色模式的用户）→ 信标的日间版本：浅蓝灰基座（如 `#EEF1F4` 一类，具体取值留给实施计划做无障碍对比度校验），文字/边框反相，但保留同一组青/铜信号色相，不能变成另一套无关配色（例如之前"静航"方向的马林蓝）——这样三档皮肤仍然是"同一个身份的不同亮度"，不是三个不同产品。
- `business-blue` → 收窄成信标语言内部的一个偏靛蓝的信号变体（比如信号色从青偏移到靛蓝），供需要区分租户/场合的场景使用，具体数值同样留给实施计划里做对比度校验后再定，这里只定方向。

这一段是本规格里我做的产品判断（保留三档皮肤机制，只重定义取值），不是用户已经确认过的决定——**实施计划开始前需要用户过一遍这一节，确认要保留皮肤切换功能，还是趁着这次改造直接收成单一固定深色身份**（后者更符合信标"刻意选择单一视觉世界"的气质，但会移除一个已上线的用户可见功能，需要用户明确同意才能做）。

## 范围与改造对象

前台（`frontend/src/components/`、`frontend/src/pages/ChatPage.tsx`）与后台（`frontend/src/admin/` 下全部页面与共享组件，含 `schemaEtlConfigBuilder/` 子目录）统一改造，不做区分。基础设施改动集中在 `frontend/src/styles/index.css` 与 `frontend/tailwind.config.ts` 两个文件，之后是约 30 个消费这些 token 的组件/页面文件（`rounded-*` 161 处引用、`shadow-soft*` 143 处引用、`accent-*` 40 处引用，分布在这批文件里）。

## 已知缺口 / 留给实施计划的决策

- `default`/`business-blue` 两档皮肤的具体色值、以及"是否保留三档皮肤机制"本身，需要用户在实施计划开始前明确（见上一节）。
- 字体加载方式（Google Fonts CDN `<link>` vs 本地托管字体文件）未定。
- 圆角/阴影改造是逐文件精确匹配旧值替换，还是允许在具体组件上做视觉判断调整（比如某个卡片可能需要比 token 表更小的圆角），实施计划需要给出统一规则，避免 30 个文件各自发挥出现不一致。
- 无障碍对比度：新色板（尤其是浅色 `default` 皮肤的最终取值）需要过 WCAG AA（正文 4.5:1，大字 3:1）校验，本规格未做这一步计算，留给实施计划。
