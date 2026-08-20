# Schema ETL 配置构建界面 设计方案

日期：2026-08-20

## 背景

"结构化数据加工"（Schema ETL）现在要求用户手写完整的 `config.yaml`（见
`app/graphrag/schema_etl_config.py` 的结构、`docs/superpowers/specs/2026-08-16-schema-etl-engine-design.md`
的完整设计、`docs/schema-etl-wide-table-guide.md` 的宽表用法）。此前做的"生成示例数据"
功能（`app/graphrag/schema_etl_sample.py`）只帮用户"看懂格式"，产出的是范例数据，不是
用户自己真实列的映射。

本方案是一个新的、独立的能力：**让用户对着自己真实数据的列，通过界面一步步配出属于
自己的 config.yaml**，不需要手写 YAML。

## 决策记录（本次 grill-me 访谈定下）

1. **先读表头，下拉选择列名**：用户在界面里"添加"本地数据文件时，前端用浏览器
   File API 直接读取该文件的第一行解析出表头（不需要把整个文件传到后端），后续
   配置各字段时都是从这份表头下拉选择列名，不允许手填未经校验的列名。
2. **文件与映射解耦**：用户先把要用到的本地文件都"添加"进这次会话（每个文件只读
   一次表头），每条实体/关系映射各自有一个"选文件"下拉框，选项是已添加文件列表，
   **没有唯一性限制**——两条映射选中同一个文件是合法的（对应宽表场景，见
   `docs/schema-etl-wide-table-guide.md`），选中不同文件也合法（对应每类型各自一个
   文件的常规场景）。两条映射选中同一文件时复用同一份已读表头。
3. **`node_key_parts` 默认简单、进阶折叠**：每条实体映射的 node_key 配置默认是
   "从该映射选中文件的表头里选一列"（对应 `{column: ...}`），支持点"添加另一列"
   叠加成复合 key（因为 `node_key_parts` 本身是列表）。每一列旁边有一个"这一列没有
   现成唯一编码？"的展开链接，点开后才出现 `allocated_code` 的进阶配置（作用域列
   可多选 + 原始值列，均从同一份表头选）。默认不展开，不打扰不需要这个能力的用户。
4. **关系映射按"已确认三元组"整体选择，不分两步**：不做"先选 relation_type 再选
   subject/object 类型"这种两步流程。界面把已确认的
   `(subject_term_type, relation_type, object_term_type)` 三元组整体作为一个可选
   单位，列表项显示成 `{subject} —{relation_type}→ {object}` 的形式，选中一项同时
   确定三个字段。实体映射的 `term_type` 选择同理，下拉只列已确认的 term_type。**不
   需要额外的实时校验/灰化逻辑**——不合法的组合根本不会出现在可选列表里，结构性
   排除了"配出一份引用未确认 schema 的 config"这类错误。
5. **前端生成 YAML，复用现有提交接口**：`config.yaml` 的文本内容完全在浏览器里拼
   出来（生成规则见下），不经过后端。"确认"提交时，把生成的 YAML 包成一个
   `Blob`/`File`，把用户已经添加的那些本地 `File` 对象，按现有
   `POST /api/admin/{tenant_id}/schema-etl/runs` 接口要求的 `multipart/form-data`
   形状（`config` 字段 + `data_files` 字段）直接提交——跟现有裸上传表单提交的形状
   完全一样，`start_schema_etl_run`（`app/api/admin_schema_etl_routes.py`）不需要
   知道这份 YAML 是手写的还是界面生成的，**这一步不需要新增/修改任何后端接口**。
   提交前必须有一个只读的 YAML 预览区块，复用"查看示例数据"那次做的 `<pre>` 纯文本
   展示风格，让用户在点确认前能看到最终生成结果。
6. **本体数据源复用现有接口，不需要新增**：探明现有
   `GET /api/admin/ontology/{tenant_id}/term-types?status=confirmed`
   （`app/api/admin_ontology_routes.py:66-81`，返回
   `{term_types: [{value, extra_fields: [{name, value_type}]}]}`）和
   `GET /api/admin/ontology/{tenant_id}/constraints?status=confirmed`
   （同文件 337-352 行，返回
   `{constraints: [{subject_term_type, relation_type, object_term_type}]}`）已经
   返回本功能需要的全部本体数据，字段结构与本方案的需求完全匹配。**结论：本功能
   大概率完全不需要新增任何后端代码，是一个纯前端功能**——具体到实现阶段如果发现
   真的有欠缺，再评估要不要补一个新接口，不预先假设需要。
7. **与现有裸上传表单并存，不替代**：新配置界面服务"对着自己列从零搭建映射"这个
   主流场景；现有的裸 `config`/`data_files` file input 上传表单继续保留，服务"已经
   有一份验证过的 config.yaml、直接传文件更快"的老手/迁移场景。两条路径在页面上
   并列展示，谁用哪条自己选。
8. **不支持导入已有 config.yaml 回填界面编辑**：只支持从零开始搭建。反向解析任意
   手写 YAML 到界面可编辑状态的成本和收益不成正比（服务的人群和"从零搭建"人群不
   重合），如果后续有真实需求再单独评估，不预先建设。

## YAML 生成实现方式

不新增 `js-yaml` 一类的第三方依赖——`config.yaml` 的产出形状是本方案自己完全控制的
固定结构（`tenant_id` 字符串 + `entities`/`relations` 两个对象数组，字段都是已知的
字符串/简单列表），不需要通用 YAML 解析/序列化能力，只需要一个**手写的、只处理这一种
固定形状的 YAML 文本拼接函数**（前端项目里的 TypeScript 代码，不是新的 npm 包）。

## 涉及的现有机制（本方案的实现基础，不在本方案范围内重新设计）

- `SchemaETLConfig`/`EntityMapping`/`RelationMapping`/`ColumnNodeKeyPart`/
  `AllocatedCodeNodeKeyPart`（`app/graphrag/schema_etl_config.py`）：本方案生成的
  YAML 必须能被 `load_schema_etl_config` 正确解析，字段名和结构必须与其一致。
- `POST /api/admin/{tenant_id}/schema-etl/runs`
  （`app/api/admin_schema_etl_routes.py::start_schema_etl_run`）：本方案的提交
  路径，不修改。
- `GET /api/admin/ontology/{tenant_id}/term-types`、
  `GET /api/admin/ontology/{tenant_id}/constraints`
  （`app/api/admin_ontology_routes.py`）：本方案的本体数据源，不修改。
- `docs/schema-etl-wide-table-guide.md`：本方案要支持的"文件与映射解耦"模型的
  设计依据。
