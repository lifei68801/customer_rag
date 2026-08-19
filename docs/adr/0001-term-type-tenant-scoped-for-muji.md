# term_type 分类枚举从全局改为按租户隔离

**Status**: proposed

**更新（2026-08-19）**：本 ADR 提到的 `product_line` 全局枚举已被移除，见 docs/superpowers/specs/2026-08-19-remove-product-line-design.md。

`ontology_term_types`（`term_type` 枚举表）在 2026-08-14 的本体 schema 基座计划（`docs/superpowers/specs/2026-08-14-ontology-schema-design.md` 第 3 节）里被明确设计为全局、不分租户——因为它要跟已上线的全局 `terms` 表口径保持一致，全部 7 个任务、多轮审查都基于这个前提。

MUJI 租户接入商品知识图谱（`docs/MUJI_知识图谱_Schema设计方案_v6.md`）时，需要把 Product/SKU/Category/VariantValue 这类**结构性实体类型**也表达为 `term_type` 取值，才能复用现有 Term 节点体系而不新建平行子系统。但这些值语义上是"数据结构类型"（相当于 Neo4j 节点标签），跟其他租户已有的 `term_type` 值（如酒店客服场景的 "error_code"、"module"，纯业务分类标签）不是同一层次的概念。继续放在全局枚举里会让每个租户在后台管理界面看到所有其他租户的无关分类值，造成词表污染。

因此把 `term_type` 改为按租户隔离，`product_line` 保持全局不变（MUJI 场景无对应需求，没有理由跟着变）。

## Considered Options

- 保持全局 + 新增独立的 `entity_kind` 字段表达结构类型：能避免破坏已上线设计，但等于同时维护两套"分类"概念，MUJI 的 Product/SKU 分类实际要在两个字段间做归属判断，复杂度更高。
- MUJI 整体做成独立子系统，不共享 Term/term_type：技术上更干净，但业务方明确要求尽量复用现有体系，接受改造成本。
- **（选定）term_type 改按租户隔离**：语义上不再要求"全局唯一分类空间"，允许不同租户的 `term_type` 词表互不可见，直接解决词表污染问题。

## Consequences

- 需要新增/修改：`ontology_term_types` 表加 `tenant_id` 列并调整主键；`ontology_categories.py` 的 CRUD、级联改名、delete-protection 逻辑全部要按租户过滤；`admin_ontology_routes.py` 的 term-types 路由要从全局路径改成 `/{tenant_id}/term-types` 风格，与 relation-types/constraints 路由对齐。
- 存量数据迁移：当前已上线的全局 `term_type` 行（如 "error_code"、"module"）需要决定归属到哪个/哪些租户，或引入一个"共享默认租户"兜底，避免迁移后老租户丢失已有分类。
- `product_line` 与 `term_type` 从"同一层级的两个全局枚举"变成"隔离粒度不一致的两个枚举"，后续任何读写 Term 分类的代码都要注意两者不能再用同一套按值查询的假设。
