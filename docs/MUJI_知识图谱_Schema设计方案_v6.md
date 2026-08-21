# MUJI 知识图谱 · Schema 设计方案（v6 简化版）

> 状态：设计提案，待评审
> 基础：在原 v5 方案（12 实体 / 8 关系）上做结构简化，核心变化是
> **把"变体值的 4 种用法"从「实体类型层」下沉到「属性结构层」**
> 结果：**9 实体 / 8 关系**，能力不减，结构更简，且不再依赖非标准的多态端点特性

---

## 0. 这一版相对 v5 改了什么，为什么改

| 项目 | v5 | v6 | 改动理由 |
|---|---|---|---|
| 变体值节点类型 | 4 个（NominalValue / QuantityValue / DimensionalValue / OrdinalValue） | **1 个**（`VariantValue`） | 4 种"用法"不是身份差异，是取值结构差异，应该用字段表达，不该用实体类型表达 |
| `has_variant` 端点 | 多态（`is_union_endpoint`，指向 4 类中任一类） | **单一类型端点**，普通 N:N | 不再依赖自定义本体框架的多态扩展，标准图工具/RDF 原生支持 |
| 变体值节点主键 | `Variant:{dim_code}:{原始值文本}`（如 `Variant:dim_007:抹茶`） | `Variant:{dim_code}:{value_code}`，`value_code` 与 `dim_code` 同一套治理 | 原方案把显示值拼进主键，改名会断边，与"改名不断边"的设计初衷自相矛盾 |
| 本体 `规格` 类 | 拆 4 个子类 | **不拆**，`value_kind` 作为属性字段 | 本体定义同步变简单 |
| 用法扩展性 | 若出现第 5 种用法（如布尔/区间），需要论证要不要建第 5 个实体类型 | 只需在 `value_kind` 枚举加一个值 + 定义其结构化字段，**不碰图结构** | 更彻底地贯彻"叫法进数据、用法进结构"——这次连"用法"扩展也做成了改数据 |

---

## 1. 前提约束（沿用 v5，未变）

### 1.1 SPU 是聚合产物，不是主数据字段

| 品类 | 主数据 `sjan` : `jan` | SPU 怎么来 |
|---|---|---|
| 服饰 | 17,085 : 165,171（1:9.67） | `sjan` 天然就是 SPU |
| 生活 | 19,111 : 19,112（1:1） | 按商品名聚合 |
| 食品 | 1,904 : 1,915（1:1） | 按商品名聚合，聚合键 `{Class}\|{族名}\|{正装/替换装}\|{plan_year}` |

`Product` 的身份是 `spu_products.product_group_id`，不是 `sjan`。全品类都有 SPU/SKU 两层，`has_sku` 全品类适用。

> **治理要求（本版新增）**：聚合逻辑（`build_food_spu_from_master.py`、`build_hb_food_spu_by_rules.py`）是 `Product` 身份稳定性的隐性前提。聚合键变更须视为 schema 变更，走本文档的版本评审，而不能只改脚本。

### 1.2 变体维度个数与名称会变，因此维度是数据

MUJI 各 Depa 的变体维度不同（食品：口味/规格/包装…；E&O：型号/电压/配件…），且未来加品类会继续增加维度、维度也可能改名。**维度不作为图结构的组成部分，而是登记表里的一行数据**，图结构里不出现任何维度的字面量名称。

---

## 2. 实体清单（9）

### 2.1 主干实体（3）

| 实体 | 是什么 | node_id | 关键字段 | 数据源 |
|---|---|---|---|---|
| `Product` | 商品（SPU） | `Product:{product_group_id}` | `product_group_name`、`md_no`、`sku_count`、`long_description`、`material`、四段类目码 | `spu_products`（聚合产出） |
| `SKU` | 商品规格 | `SKU:{jan}` | `jan`、`sjan`、`price`、在售状态、四段类目码 | `sku_master`（186,198 行） |
| `Category` | 商品类目 | `Category:{path_code}` | `path_code`、层级、名称、父节点 | `ontology_category_node`（从 L2 起建节点，L1 只是常量，不落节点或落 1 个 ROOT） |

> `Product` 骨架字段以迁移 28 后实际列为准，`brand`/`series_id`/`series_name`/`varies_by`/`category_l1-l3`/`materials`/`functions`/`usage_scenarios`/`target_users`/`measurements` 已被 DROP，改由 `spu_product_attrs`（EAV）承载。

### 2.2 变体值实体（1）—— 单一类型 + 结构化 payload

| 实体 | node_id | node_type | 数据源 |
|---|---|---|---|
| `VariantValue` | `Variant:{dim_code}:{value_code}` | `VariantValue`（唯一类型） | 聚合产出的维度值 |

**属性结构：**

```
VariantValue.props:
  dim_code        string    维度稳定码，来自 variant_dimension 登记表
  value_code      string    值稳定码，首次入库分配，永不回收
  label_cn        string    显示名，可随时改，不影响 node_id
  value_kind      enum      nominal / quantity / dimensional / ordinal（可扩展）
  raw_value       string    原始文本，名称类的比对字段
  numeric_value   number    数量类：拆出的数字（如 500）。仅 value_kind=quantity 时有效
  unit            string    数量类：单位（如 ml）。仅 value_kind=quantity 时有效
  dims            number[]  尺寸类：拆出的各边数字（如 [200,140]）。仅 value_kind=dimensional 时有效
  dims_unit       string    尺寸类的单位（如 cm）
  order_rank      integer   档位类：排序号（S=1/M=2/L=3）。仅 value_kind=ordinal 时有效
  status          enum      active / deprecated
```

**各 `value_kind` 只使用对应的结构化字段，其余置空**：

| value_kind | 说人话 | 举例 | 用哪些字段回答什么问题 |
|---|---|---|---|
| `nominal`（名称类） | 一个名字，只能对得上或对不上 | 口味=抹茶、颜色=黑色、包装=套装 | `raw_value` 精确匹配 →「有抹茶味的吗」 |
| `quantity`（数量类） | 一个数字+单位，能比大小 | 净含量=70g、容量=750ml | `numeric_value`+`unit` 范围过滤 →「有 500ml 以上的吗」 |
| `dimensional`（尺寸类） | 几个数字组合，判断放不放得下 | 床品=200×140cm | `dims` 逐边比较 →「能塞进 80cm 空隙吗」 |
| `ordinal`（档位类） | 有大小顺序但非数字 | 尺码 S/M/L/XL | `order_rank` 大小比较 →「比 M 码大的有哪些」 |

**未来出现第 5 种用法怎么办**（原方案未回答的问题，本版补齐）：在 `value_kind` 枚举追加一个值，并定义它对应哪些结构化字段（可复用已有字段，或在 `props` 里加新字段）——这是**数据/字段层的扩展，不新建实体类型，不改图结构**。例如未来若需要"区间类"（如"适用月龄 0-6 月"，判断某值是否落在区间内），只需加 `value_kind='range'` + `range_min`/`range_max` 两个字段。

**"电气/兼容"拆分（沿用 v5 结论）**：该维度原来混了"电池型号"（名称类）和"电压/节数"（数量类）两种用法，一个维度只能对应一种 `value_kind`，需拆成两个 `dim_code`。

**"材质/面料"不当变体值（沿用 v5 结论）**：其取值（棉/麻/法兰绒）已有 `Material` 实体及独立档案，走 `has_material` 关系，不建 `VariantValue` 节点，避免同一概念在图里出现两份。

### 2.3 归属实体（2）

| 实体 | node_id | 源列 | 说明 |
|---|---|---|---|
| `Series` | `Series:{md_no}` | `spu_products.md_no` | N:N；覆盖 ~94.8%。以 `md_no`（品番）为唯一来源 |
| `Material` | `Material:{material_id}` | `material_profile` + `spu_material_link` | 有独立档案（功能特性/洗护/可持续性/商业定位） |

> **不建 `Brand`**：全品类同属 MUJI 一个品牌，单值维度无区分度；`spu_products.brand` 已被迁移 28 DROP。待出现第二品牌（如 IDÉE）再建。

### 2.4 通用维度实体（3）

| 实体 | node_id | 源列 | 去重值（服饰/生活/食品） | 为什么值得建 |
|---|---|---|---|---|
| `Season` | `Season:{value}` | `season_name` | 10 / 7 / 7 | 基数极低、三品类通用、受控词表 |
| `Origin` | `Origin:{value}` | `country_of_origin_local` | 15 / 38 / 9 | 「日本产的有哪些」 |
| `TargetGender` | `Gender:{value}` | `sex` | 5 / 5 / 0 | 「男装有哪些」；食品不适用，关系标记为可选 |

**实体合计：`Product`、`SKU`、`Category`、`VariantValue`、`Series`、`Material`、`Season`、`Origin`、`TargetGender` = 9（`TargetGender` 若不启用则 8）。**

---

## 3. 登记表设计

### 3.1 `variant_dimension`（维度登记表）

| 字段 | 说明 |
|---|---|
| `dim_code` | 稳定码（如 `dim_007`），分配后永不变，节点与边只引用它 |
| `label_cn` | 显示名，可随时改 |
| `depa` | 所属品类 |
| `family` | 归一族，跨品类聚合查询用（如"物理尺寸""包装"横跨全部 Depa） |
| `value_kind` | 该维度的值属于哪一类（nominal / quantity / dimensional / ordinal / …） |
| `is_primary` | 是否该 Depa 的主变体轴 |
| `status` | active / deprecated（废弃不删除，边随之停建） |
| `override_reason` | **本版新增**：允许业务优先级高但未达统计门槛的维度手动登记，注明理由 |

### 3.2 `variant_value_registry`（值登记表，本版新增，替代把值拼进 node_id）

| 字段 | 说明 |
|---|---|
| `value_code` | 稳定码，首次入库分配，永不回收 |
| `dim_code` | 所属维度 |
| `label_cn` | 显示名，可随时改，不影响 `value_code` |
| `status` | active / deprecated |

> 有了这张表，`VariantValue` 节点的身份完全脱离显示文本，彻底解决"改名断边"问题（原 v5 方案只解决了维度层的改名问题，未解决值层）。

### 3.3 维度准入门槛（决定要不要注册进 `variant_dimension`）

**准入门槛：该维度取值的唯一率 < 5% 且非空覆盖 > 50%**（门槛卡在聚合后的维度值上，不是原始列上——聚合前有大量占位符脏值，如食品 `color` 字段 85% 是 ETC 占位值）。

| 列（实测） | 服饰 | 生活 | 食品 |
|---|---|---|---|
| `color_name` | 0.8% ✅ | 7.9% ⚠️ | 49.4% ❌ |
| `sz_name` | 1.1% ✅ | 37.5% ❌ | 23.8% ❌ |
| `season_name` | <0.1% ✅ | <0.1% ✅ | 0.4% ✅ |

达标 → 登记维度，标明 `value_kind`，值建成 `VariantValue` 节点。不达标 → 该维度只落 L1 属性（`spu_product_attrs`），不建节点。判断粒度细到单个维度，同一 Depa 内互不影响。

`override_reason` 字段用于纯统计规则误伤有业务价值的维度时（如新品类样本量小导致唯一率虚高）的人工兜底。

---

## 4. 关系清单（8）

| # | 关系 | 起点 → 终点 | 基数 | 取哪列 | 适用品类 | 答什么 |
|---|---|---|---|---|---|---|
| 1 | `has_sku` | Product → SKU | 1:N | `product_group_id`→`jan` | 全 | 有哪些规格 |
| 2 | `belongs_to_category` | Product → Category | N:1 | →`sel_class` | 全 | 哪个小类 |
| 3 | `contains_child` | Category → Category | 1:N | 类目树 `parent_id` | 全 | 类目导航 |
| 4 | `has_variant` | SKU → **VariantValue**（单一类型） | N:N | 聚合产出的维度值 + `dim_code` | 全 | 有什么颜色/口味/尺寸/规格…一条关系覆盖全部维度，**普通端点，无需多态特性** |
| 5 | `has_material` | Product → Material | N:N | `spu_material_link`（带 `role`） | 服饰/生活 | 什么面料 |
| 6 | `part_of_series` | Product → Series | N:N | →`md_no` | 全 | 同系列还有啥 |
| 7 | `in_season` | Product → Season | N:1 | →`season_name` | 全 | 有哪些春夏款 |
| 8 | `from_origin` | Product → Origin | N:1 | →`country_of_origin_local` | 全 | 日本产的有哪些 |

`suitable_for_gender`（Product → TargetGender）可选，服饰/生活适用，食品 `sex` 为 0%。

> **`has_variant` 本版改动**：端点不再是"4 类中任一类"，而是固定指向 `VariantValue` 一种类型，用 `props.value_kind` 区分用法。**不需要 `onto_relation.is_union_endpoint` 这类多态端点扩展**，是标准的单一目标类型 N:N 关系，任何图数据库/RDF 工具原生支持。

**索引要求**：变体类问法需带 `dim_code` 过滤，索引打在 `graph_nodes.props->>'dim_code'`；数值比较类问法（数量类/尺寸类/档位类）还需在 `numeric_value`、`dims`、`order_rank` 上建索引。

### 典型问答走法

```
唇膏都有什么颜色    Category → Product → SKU --has_variant--> V(value_kind=nominal, dim=颜色)   GROUP BY
有 500ml 以上的吗   SKU --has_variant--> V(value_kind=quantity, numeric_value>500)
比 M 码大的有哪些   SKU --has_variant--> V(value_kind=ordinal, order_rank>2)
能塞进 80cm 空隙吗  SKU --has_variant--> V(value_kind=dimensional, dims 全<80)
这个咖喱什么口味    SKU --has_variant--> V(dim=口味)
那个红色的多少钱    V(dim=颜色, raw_value=红) ← SKU（取 price）
这个 JAN 是什么     SKU → Product → Category
童装有没有法兰绒    Category(前缀) → Product → Material
有哪些春夏的男装    Season ← Product → TargetGender
同系列还有什么      Product → Series → Product
```

---

## 5. 设计原理

### 5.1 关系按类建，不按列建

字段字典 146 个 `field_code`，按列两两配对是 O(146²)，永不收敛；按业务类配对，类只有个位数。例：`sku_has_size`+`sku_has_sz`+`spu_lists_sizes` 三列并入 `has_variant` 一条关系，三列变成三行取数配置 + 一个 `dim_code`。材质同理：六条材质特性关系收敛为 `material_profile.functional_properties` 一个字段。**30 条 → 8 条，业务含义一条不少。**

### 5.2 列的差异不丢，下沉成取数配置

| rel_code | 品类 | from | to |
|---|---|---|---|
| `has_variant` | 默认 | `jan` | `sz_name` → `dim_code=物理尺寸` |
| `has_variant` | 服饰 | `jan` | `size` → `dim_code=尺码档位` |
| `has_variant` | H&B | `jan` | `hb1_specification` → `dim_code=规格/数量` |

加品类 = 加一行取数配置，不新建关系。

### 5.3 子类型用 `role`，不用新关系

`has_material` 一条，用 `spu_material_link.role` 区分主体/罗纹/里料/口袋布，收敛掉原来的三条关系。**通用规则：想加关系但目的只是区分"同一种连接的不同子类型"时，加的是 `role` 值，不是新关系。**

### 5.4 变体值为什么要建节点（而非属性字符串）

判据是**需不需要反查**、**是否受控词表被大量复用**（详见 §5.4.1、§5.4.2）。

**5.4.1 反查判据**：只需正向读取（"这个 SKU 是什么颜色"）→ 属性字符串足够；需要反向查找（"哪些商品是黑色的"）→ 属性字符串无法高效反查，节点化后反查就是一次图遍历（`Category → Product → SKU --has_variant--> V(颜色=黑)` 一次 `GROUP BY`），不需要预先算好存起来，避免"预算的会过期"（SKU 一改就要全量重刷）。

**5.4.2 受控词表判据**：颜色、型号这类值被海量 SKU 复用，符合"值集合有限、被大量行共享"的维表信号，唯一率门槛（<5%）就是这个判据的量化。反过来 `long_description` 这类自由文本、`price` 这类时态数据（§5.5）唯一率高或语义不适合节点化，不建节点。

**代价是真实的**：186,198 行 SKU、约 89 万条边，需要 `variant_dimension`/`variant_value_registry` 两张治理表配合。如果业务上从不需要反查（比如颜色只是详情页展示），做成属性反而更合理，节点化是过度设计。

### 5.5 `Price` 不建节点

价格是时态数据（本体类 `商业信息` 治理原文："作为时态事实存储，不直接固化到商品永久属性"）。作 `SKU` 骨架字段，查询时直接取，省去 ~17 万条边和改价重建边的成本。

### 5.6 类目 L1 是常量

`sel_div` 三品类都只有 1 个去重值，无区分度，`Category` 从 L2 起建节点。层级基数：L2 `sel_depa` 5~8 / L3 `sel_line` 19~51 / L4 `sel_class` 72~354。

### 5.7 新场景怎么接（三级扩展，多数场景停在 L0/L1）

| 级别 | 做什么 | 改动 | 能答什么 |
|---|---|---|---|
| **L0** | 靠原文向量/BM25 召回 | 零 | 「注意事项是什么」 |
| **L1** | `spu_product_attrs` 加一行 `field_code` | 零 DDL | 「哪些商品可水洗」（可过滤聚合） |
| **L2** | 建节点 + 关系 | 零 DDL，插配置行 | 「可水洗的棉质童装」（多跳） |

**新概念该建成什么：**

```
新概念进来
  ├─ 有自己的属性档案吗（不止 id + 名字）？        是 → 实体
  ├─ 需要反查吗（"哪些商品是这个值"）？            是 → VariantValue（且过准入门槛）
  │    └─ 取值方式不属于现有 value_kind？          → 加枚举值 + 结构化字段，不新建实体类型
  └─ 都不是 → 属性，进 L1，不进图
```

---

## 6. 二期与不建

### 二期（数据未就绪）

| 实体 | 关系 | 现状 |
|---|---|---|
| `Fit`（版型） | `has_fit`：Product → Fit | `silhouette`/`fit_note` 在属性库，端点不完整 |
| `Function`（功能） | `has_function` | 在 `spu_products.functions` JSONB，未归一化 |
| `Scenario`（使用场景） | `applies_to_scenario` | 在 `usage_scenarios` JSONB，未归一化 |
| `TargetUser`（目标人群） | `suitable_for_user` | 已有 ~1.8k 边，词表未收敛 |
| `Ingredient`（成分） | `has_ingredient` | 主数据没有，食品必需，需新数据源 |

升级判据：去重后 < ~200 个值且重复率高 → 可升 L2；否则留 L1。

### 明确不建

| 不建 | 原因 |
|---|---|
| `SIMILAR_TO` | 向量召回已够，显式边难维护易过期 |
| `REPLACES` | 无稳定主数据源 |
| Div/Dept/Line → Product 边 | 冗余；用 `path_code` 前缀检索，商品只挂 L4 |
| 材质细分边（凉感/针织/手感…） | 由 `functional_properties` 一个字段承载 |

---

## 7. 已知限制与待确认事项

1. **食品品类缺成分/过敏原/口味结构化字段**：`material`/`knit_woven`/`sex` 均 0%，`country_material_name` 仅 9 个去重值且是占位符。有值的只有 `long_description`（100%）和 `direction`（456 条风味描述）。需新数据源（包装 PDF / 官网），非建模可解决。
2. **食品日本侧字段填充率精确停在 49.9%**（`com_name_kj`/`color`/`sz`/`pb_flag_jp` 等），推测是"日本引进 vs 国内自采"区别，入库需确认非数据缺失。
3. **准入门槛的统计阈值（5%/50%）为经验值，无语义校验**：新品类初期样本小可能导致唯一率虚高、误判不达标，需靠 `override_reason` 人工兜底，建议首次接入新品类时人工复核一轮准入结果。
4. **`Product` 身份依赖聚合脚本**：聚合键变更需视为 schema 变更纳入本文档版本评审，避免图结构未变但身份定义悄悄漂移。
5. **`Brand` 本体类当前是 orphan**（不被任何关系连接）：TBox 层的登记问题，不影响运行时,待出现第二品牌再建关系。

---

## 8. 版本对照总表

| | v5 | v6（本版） |
|---|---|---|
| 实体数 | 12（含 4 个变体值类型） | **9**（1 个变体值类型） |
| 关系数 | 8 | 8 |
| `has_variant` 端点 | 多态（4 类任一） | 单一类型，普通 N:N |
| 变体值节点 ID | 拼入显示文本，改名断边 | `value_code` 独立登记，改名不断边 |
| 用法扩展方式 | 需论证是否建第 5 个实体类型 | 加枚举值 + 结构化字段，不碰结构 |
| 本体 `规格` 类 | 拆 4 个子类 | 不拆，`value_kind` 作属性 |
