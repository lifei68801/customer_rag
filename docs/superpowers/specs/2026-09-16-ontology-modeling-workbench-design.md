
**状态**：设计
**日期**：2026-09-16
**前置**：2026-09-15 数据源解析选项（多表读取的地基）

## 背景

本体建模是知识图谱实施最重要、也最难的前提。企业通常不知道该建什么，也不知道手里的数据和文档能怎么帮自己建。本项目今天给企业的只有两条路，两条都不解决这个问题：

| 路径 | 今天的做法 | 天花板 |
|---|---|---|
| 手工 | 「本体结构」页逐个建实体类型、关系类型、约束 | 企业不知道该建什么，这条路等于没有 |
| 引导建模 | 传**一张**表 → `columnStats` 扫全表算每列基数 → `columnRoles.ts` 按比例分角色 → `draftProposal.ts` 拼草案 → `draft/replace` 整份替换 | 见下 |

引导建模的天花板是结构性的，代码里看得很清楚：

- **实体是从一张表的列基数推出来的。** `DIMENSION_MAX_RATIO = 0.2` 判的是"这列像不像维度"，不是"这个概念在业务里是不是一个实体"。
- **关系语义是空的。** `suggestRelationName` 产出 `HAS_<列名>`，中文列名直接退回 `RELATES_TO`。
- **纯前端、纯启发式、零 LLM。** 后端有完整的 provider registry，引导建模一次都没用。
- **文档不参与。** `llm_extractor.py` 是封闭世界的——提示词写死"只能抽已确认本体里的类型，不是建议，是硬约束"。文档只是拿已有本体去抽事实的下游消费者，从不反过来贡献本体。
- **单表。** 多表之间的主从、外键、事实/维度关系没有任何表达。
- **中间态不持久化。** 全部状态在前端内存里，刷新即丢。

数据模型本身是够用的：`term_types`（`extra_fields`、`node_key_template`、`standard_name_value_type`）、`relation_types`（`example_phrase`、`description`、`allow_chain_query`、`source`）、`constraints`（主语 × 关系 × 宾语的允许组合），加上 draft → confirmed 生命周期和变更日志。缺的不是表达力，是**得到这份本体的过程**。

第二个诉求：做完一个领域（消费品零售）之后，方法论要能沉淀成可复用的东西，下一个同领域客户直接用。平台里有一个可借鉴的先例——`app/agent/tool_registry.py` 扫 `app/agent/tools/*/manifest.yaml` + `tool.py` 在启动时注册——但它是代码级、跟着部署走的，不是"从一个做完的租户本体反向导出"。

## 目标

1. 企业能从**一份领域骨架**起步，而不是从空白或从一张表的列名起步。
2. 数据和文档的作用从"猜本体"变成"给骨架找证据"：每个本体元素都能回答"你从哪来、有没有真实数据支撑"。
3. 建模过程可中断、可续上、可迭代；确认之后发现漏了，能回到同一个地方继续。
4. 做完一个领域，能导出成下一个客户可用的内置 skill。

## 非目标

- 不做租户自助发布 skill 到平台（见决策 5）。
- 不做两份本体的自动合并（见决策 8）。
- v1 不做 LLM 冷启动、文档发现、问题清单、问答对未落地类型的解释（见决策 11，全部为 v2）。

## 核心概念

**Skill（领域模板）**：一份声明式 YAML，描述某个领域的候选本体骨架、典型业务问题、以及从企业列名/术语认出骨架概念的匹配线索。内置、跟代码发版，格式见"Skill 格式"一节。

**建模工作区（Modeling Workspace）**：一个租户一份、长期存在的过程状态，存骨架、每个元素的来源、旁证、候选、审阅决定。它**不是**本体草稿；"应用到草稿"是一个显式动作。叫"工作区"不叫"会话"：代码库里"会话"已经指登录会话（`AdminSession`）和前台聊天会话两样东西，它跟这两者都无关——不随登录失效、不属于某个用户，跟租户的本体同寿。

**来源（provenance）**：本体元素是谁提出来的。取值 `skill` / `llm` / `data` / `document` / `question` / `manual`。v1 只出现 `skill` / `data` / `manual`。

**落地状态（grounding）**：这个实体类型/关系类型有没有真实数据支撑。**纯推导**：ETL 映射（`ontology_etl_mapping.config_yaml` 里的 `entities` / `relations`）里有一条指向它就是落地，否则未落地。不存、不缓存。

**旁证（clue）**：不改变落地状态、只用于排序和解释的线索。文档提及次数与原文片段、人工标注（"我们有，数据下个月接"）。v1 只有人工标注一种。

## 三阶段流程（全貌）

```
阶段一  骨架              阶段二  校准              阶段三  证据
┌─────────────┐        ┌─────────────┐        ┌─────────────────┐
│ 选内置 skill │        │ 业务问题清单  │        │ 多表数据 → 对齐骨架│
│   或         │───────▶│ 问题→所需元素 │───────▶│ 文档 → 概念候选   │
│ LLM 现场生成 │        │ 标出覆盖缺口  │        │ 落地状态推导      │
└─────────────┘        └─────────────┘        └─────────────────┘
       │                      │                        │
       └──────────────────────┴────────────────────────┘
                              ▼
                     建模工作区（持久化，可续）
                              │  应用（带 diff）
                              ▼
                         本体草稿 ──确认──▶ 已确认本体 ──▶ 验收：问题清单跑问答
                              ▲
                      「本体结构」页手工改动
```

v1 只做阶段一的 skill 分支和阶段三的数据分支（阴影部分见决策 11）。三个阶段对同一个元素各有意见时的仲裁规则只有一条：**任何来源提出的元素都进工作区，落地与否由 ETL 映射说了算，未落地的不阻塞、不删除、持续可见**（决策 2）。

## 数据模型

### 新表 `ontology_modeling_workspaces`

```sql
CREATE TABLE IF NOT EXISTS ontology_modeling_workspaces (
    tenant_id     TEXT NOT NULL PRIMARY KEY,
    skill_name    TEXT,            -- 起步用的 skill；NULL = 空白起步
    skill_version TEXT,
    state_json    TEXT NOT NULL,   -- 见下
    updated_at    TEXT NOT NULL,
    updated_by    TEXT NOT NULL
);
```

一个租户一份（决策 10：工作区长期存在，不是一次性的）。`state_json` 结构：

```jsonc
{
  "term_types": [
    {
      "value": "SKU",
      "display_name": "商品",
      "provenance": "skill",              // skill | data | manual（v2 加 llm | document | question）
      "review": "accepted",               // pending | accepted | rejected
      "extra_fields": [...],              // 同 term_types.extra_fields 的形状
      "standard_name_value_type": "string",
      "clues": [                          // 旁证，不影响落地
        { "kind": "manual", "note": "数据下个月接", "by": "alice", "at": "..." }
      ],
      "data_match": {                     // 阶段三数据发现的对齐结果，可为 null
        "source_file": "CN_001_SKU_MASTER_121.xls",
        "key_columns": ["jan"],
        "field_columns": { "color": "color", "size": "size" },
        "matched_by": "alias:jan"         // 怎么对上的：alias:<别名> | column_role | manual
      }
    }
  ],
  "relation_types": [ { "relation_type": "BELONGS_TO_CATEGORY", "provenance": "skill", "review": "pending", ... } ],
  "constraints":    [ { "subject": "SKU", "relation": "BELONGS_TO_CATEGORY", "object": "Category", "provenance": "skill", "review": "pending" } ],
  "unmatched_columns": {                  // 数据里有、骨架里没接住的列，供用户提升为新元素
    "CN_001_SKU_MASTER_121.xls": ["md_no", "brand_cd", ...]
  },
  "questions": []                         // v2
}
```

落地状态**不在**这里——它每次从 ETL 映射推导（决策 7）。`data_match` 是"发现时对上了哪列"的记录，应用到草稿时会生成 ETL 映射，之后落地状态就由那份映射说了算；`data_match` 只是历史。

### 本体表不加列

`term_types` / `relation_types` / `constraints` 一列不加（决策 9）。工作区是过程，本体表是结果。

## Skill 格式

放在 `app/ontology_skills/<name>/skill.yaml`，启动时扫描注册，格式错误直接失败（照搬 `tool_registry` 对 manifest 的态度：一个格式错误的 skill 静默跳过比启动失败更危险）。

```yaml
name: consumer_retail
version: 1
display_name: 消费品零售
description: 面向品牌方/零售商的 SKU 主数据、门店、订单、促销骨架。源自 2026-09 MUJI 项目。

term_types:
  - value: SKU
    display_name: 商品
    standard_name_value_type: string
    extra_fields:
      - { name: color, value_type: string, display_name: 颜色 }
      - { name: size,  value_type: string, display_name: 尺码 }
      - { name: retail_price, value_type: number, display_name: 零售价 }
    # 阶段三数据发现用的匹配线索。别名命中列名（不区分大小写、忽略下划线与空格）
    # 即对上；命不中时 v2 再交给 match_hint 里的提示词。
    key_aliases: [jan, sku, sku_code, item_cd, 商品编码, 品番]
    field_aliases:
      color: [color, colour, 颜色, 現地語色]
      size:  [size, 尺码, サイズ]
      retail_price: [retail_price, price, 零售价, 売価]
  - value: Category
    display_name: 品类
    key_aliases: [category, cat_cd, 品类, 大分类]
  - value: Store
    display_name: 门店
    key_aliases: [store, store_cd, shop, 门店, 店铺]

relation_types:
  - relation_type: BELONGS_TO_CATEGORY
    example_phrase: 某商品属于某品类
    description: SKU 到其所属品类
  - relation_type: SOLD_AT
    example_phrase: 某商品在某门店有售

constraints:
  - [SKU, BELONGS_TO_CATEGORY, Category]
  - [SKU, SOLD_AT, Store]

questions:          # v2 用；v1 允许存在但不读
  - 哪个品类上个月销量最高？
  - 某个 SKU 在哪些门店有售？

match_hint: |       # v2 用；可选的提示词片段
  这个领域的商品编码常见叫法：JAN、品番、SKU、ITEM_CD……
```

`term_types` / `relation_types` / `constraints` 的字段与后端 `draft/replace` 的 payload 一一对应，多出来的只有匹配线索和问题清单。这保证了**导出**可以做成纯机械操作（决策 6）。

## 行为规格（v1）

### 1. 起步

工作台首页：列出内置 skill（名字、描述、版本、包含多少实体/关系），或"空白起步"。选定后创建工作区，骨架里每个元素 `provenance=skill`、`review=pending`。已有工作区时直接进入工作台。

### 2. 骨架审阅

每个元素一行：名字、来源标、落地标（从 ETL 映射推导）、审阅状态、旁证数。操作：接受 / 拒绝 / 改名 / 加人工旁证 / 手工新增（`provenance=manual`）。拒绝的元素留在工作区里（折叠），不进草稿——用户回头能看到"这个我拒过"。

### 3. 数据发现（多表）

上传若干表（走 2026-09-15 的解析选项，每张表可选 sheet 与表头行）。对每张表：

1. `columnStats` + `columnRoles` 照旧算列角色。
2. 对骨架里每个 `review != rejected` 的实体类型，用 `key_aliases` 撞列名；命中即 `data_match.key_columns`，`matched_by = alias:<别名>`。再用 `field_aliases` 撞其余列填 `field_columns`。
3. 没被任何实体接住、且列角色是 `identifier` 或 `dimension` 的列，进 `unmatched_columns`，界面上以"数据里还有这些，骨架里没有"列出，用户可一键提升为新实体类型（`provenance=data`）。
4. 表之间的关系（决策 13）：两张表若被同一个实体类型的 `key_aliases` 各命中一列，且其中一张表该列的角色是 `identifier`、另一张是 `dimension`，提议一条关系（主语 = dimension 那张表所属实体，宾语 = identifier 那张表所属实体），`provenance=data`、`review=pending`，关系名取骨架里主宾匹配的约束，没有则 `RELATES_TO`。不做值级 join 分析。

命中规则是**确定性的**（别名归一化后精确匹配），命不中就命不中，不猜。这一步失败的代价是用户手动指一下列，比猜错列进图谱便宜得多。

### 4. 落地状态

工作台每次打开、每次应用之后重新推导：读当前租户的 ETL 映射（草稿优先，没有则已确认），`entities[*].term_type` 与 `relations[*].relation_type` 出现即落地。未落地元素以灰标显示，聚合成一个"未落地清单"面板——这就是下一批该接什么数据的待办。

### 5. 应用到草稿

1. 把工作区里 `review=accepted` 的元素（含 `manual` 新增）投影成 `draft/replace` 的 payload；有 `data_match` 的实体同时投影成 ETL 映射的 `entities` 条目（`source_file`、`node_key_parts=[{column: key_columns[0]}]`、`field_mappings`）。
2. 读当前草稿，算 diff：会加什么、会删什么、会改什么。**删除项单独醒目列出**——那多半是用户在「本体结构」页手工加的（决策 10）。
3. 用户确认后调 `draft/replace`（含 `etl_mapping`）。
4. 后续"确认本体"走既有流程，不变。

### 6. 导出为 skill

后台动作「导出为领域模板」：读当前租户**已确认**的本体 + ETL 映射，写成 Skill 格式 YAML 下载。`key_aliases` 用 ETL 映射里实际用到的列名填第一个值，`field_aliases` 同理；`questions` 为空；`description` 里写明来源租户与日期。产物由人工审阅、脱敏、补别名后提交进 `app/ontology_skills/`（决策 6）。**不做**自动入仓。

## 接口

全部挂在 `tenant_scoped` 下（`require_tenant_access`），跟本体路由同一套门禁。工作区读写不要求 admin 角色（跟 `59bd79b` 对引导建模的处理一致）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/ontology-skills` | 内置 skill 列表 |
| GET | `/api/admin/ontology/{tenant}/modeling-workspace` | 读工作区；不存在返回 404 |
| POST | `/api/admin/ontology/{tenant}/modeling-workspace` | 建工作区（body: `skill_name` 或空） |
| PUT | `/api/admin/ontology/{tenant}/modeling-workspace` | 整份写回 state_json（乐观锁：带 `updated_at`，不匹配 409） |
| POST | `/api/admin/ontology/{tenant}/modeling-workspace/discover` | 上传多表 + 解析选项，返回对齐结果，**不写工作区**（前端合并后 PUT） |
| GET | `/api/admin/ontology/{tenant}/modeling-workspace/grounding` | 从 ETL 映射推导的落地状态 |
| POST | `/api/admin/ontology/{tenant}/modeling-workspace/apply-preview` | 返回 diff，不写 |
| POST | `/api/admin/ontology/{tenant}/modeling-workspace/apply` | 调 `replace_draft`，带 diff 摘要写变更日志 |
| GET | `/api/admin/ontology/{tenant}/export-skill` | 导出 YAML |

`discover` 不写工作区而是把结果交回前端合并，是因为对齐结果要跟用户已经做过的审阅决定合并（不能把用户改过名的元素覆盖回去），这个合并在前端做、由用户看着做。

## 前端

工作台（决策 12：工作台而非线性向导）替换今天的「引导建模」页，路由不变。四个面板：**骨架**（审阅列表）、**数据**（上传表、看对齐、提升未接住的列）、**未落地清单**、**应用**（diff + 按钮）。顶部一行"下一步建议"：工作区刚建时是"审阅骨架"，骨架全审完是"上传数据"，有未审的数据候选是"处理候选"，有 accepted 但未应用是"应用到草稿"。

今天的 `columnStats.ts` / `columnRoles.ts` 原样复用；`draftProposal.ts` 里"从列推整套本体"的逻辑不再是主路径，保留为"空白起步 + 只传表"时的退化路径。

## 决策记录

| # | 决定 | 理由 | 代价若判断错 |
|---|---|---|---|
| 1 | 起点是三阶段（骨架 → 校准 → 证据），不是单一驱动 | 单表列基数推不出业务实体；纯模板有鸡生蛋问题；纯问题清单对不会提问的企业起不了步 | 三套机制都要建 |
| 2 | 模板提出但数据里无证据的元素：保留、标未落地、不阻塞 | 企业分批接数据是常态，删掉就要反复重建迁移；本体本该比数据活得久 | 未落地元素对下游要有明确语义（见 3） |
| 3 | 未落地类型参与问答规划，空结果说出真实原因（v2） | "没找到"和"数据还没接"是两回事，后者是给管理员的信号 | 问答链路多一处依赖 |
| 4 | skill = 声明式骨架 + 可选提示词片段 | 骨架可导出可 diff；别名词典写不完、纯提示词不可复现，两者互补 | 两种介质测试策略分开写 |
| 5 | 只有内置 skill，用户不能自助沉淀发布 | 租户对应不同客户；本体骨架是业务结构，跨客户一键分享 = 泄漏 | 每个新领域要发版 |
| 6 | 提供导出工具，产物人工审阅后入代码仓 | 几十个元素手抄慢且错；自动入仓把运行时跟发布流程绑死 | — |
| 7 | 落地状态纯推导自 ETL 映射，旁证另存 | 推导不会跟事实漂移；独立字段漏更新一处就说谎 | 文档证据只能是旁证，不改变判定 |
| 8 | 文档发现输出候选+频次+原文，对齐骨架，不合并两份本体（v2） | 本体合并没有好算法；骨架做脊柱、文档做标注 | 文档独有的层级结构表达不了 |
| 9 | 中间态存新表，应用时才写草稿 | 本体表是结果不是过程；跨多次登录、有后台任务的流程必须持久化 | 多一层心智模型 |
| 10 | 工作区长期存在，应用前看 diff | 未落地清单是下一轮入口；`draft/replace` 是整份替换，手工改动不能静默丢 | 用户每次应用要看一眼 |
| 11 | v1 = skill + 工作区 + 工作台 + 数据发现 + 落地推导 + 导出；LLM 冷启动、文档、问题清单、问答解释全 v2 | 骨架没在真实项目立住之前，加来源是白做；MUJI 就是那个真实项目 | 第一个客户只能是消费品零售 |
| 12 | 工作台而非向导 | 流程非线性（数据发现跑着时可以审骨架）、跨多次登录 | 对"不知道该干什么"的人少了牵引，用"下一步建议"补 |
| 13 | 多表关系发现只做别名撞列名 + 角色判断，不做值级 join | 确定性、可解释；猜错关系比漏一条贵 | 别名没覆盖到的表要手动指 |
| 14 | skill 记名字+版本进工作区；skill 升级不自动影响任何租户 | 已建好的本体不能因为发版变动 | 升级提示与对比是 v2 |
| 15 | 冷启动用 LLM 现场生成骨架、`provenance=llm`（v2） | 阶段二三是两道过滤，幻觉元素会显眼地挂成未落地；且正好闭环成第一版 skill | 不可复现；靠来源标区分于经验证的 skill |

## v2 路线

按依赖顺序：
1. **LLM 冷启动**（决策 15）——只加一个骨架来源，工作台不变。
2. **问题清单**——阶段二 + 验收跑问答。工作区 `questions` 字段启用。
3. **文档发现**——复用已入库 chunks，开放世界提示词，输出对齐骨架。工作区 `clues` 加 `document` 类。
4. **问答解释未落地**（决策 3）——`structured_filter_query` 读落地状态。
5. **skill 版本对比**（决策 14）。

## 风险

- **别名覆盖率**：v1 的数据发现全靠别名撞列名。MUJI 一张表能校准出一份别名表，第二个客户的列名很可能撞不上。缓解：未接住的列全部可见、一键提升；`unmatched_columns` 本身就是给下一版别名表的素材。
- **`discover` 结果与用户审阅的合并在前端**：多次发现、中途改名，合并逻辑会变复杂。缓解：元素以 `value` 为身份，改名走 `display_name`，`value` 不变。
- **工作区与草稿两套状态**：用户可能搞不清"我在工作区里改的怎么没生效"。缓解：工作台顶部常驻"有 N 处改动未应用"。
- **导出的 YAML 带客户信息**：`description`、`display_name`、别名里可能有客户特有词。这是决策 6 里"人工审阅"存在的原因，不能省。
