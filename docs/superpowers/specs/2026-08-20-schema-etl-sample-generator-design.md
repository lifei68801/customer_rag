# ETL 示例数据生成器 设计方案

日期：2026-08-20

## 背景

"数据填充 → 结构化数据加工"（ETL）功能要求用户手工准备两样东西：一份列映射配置
`config.yaml`、若干份配套 CSV 数据文件。`frontend/src/admin/SchemaEtlPage.tsx` 的上传
表单目前只有两个裸的 file input，没有任何模板、说明或范例——用户必须自己读懂
`app/graphrag/schema_etl_config.py` 的数据结构才能写出一份能跑通的配置。

本方案让系统基于租户**已确认**的本体 schema（`term_types`/`extra_fields`、
`relation_types`/`term_type_relation_allowlist`），自动生成一套**可以直接原样跑通**的
示例文件（config.yaml + 配套 CSV），供用户在页面里预览、也可以打包下载，照着改成自己
的真实数据。

## 决策记录（本次 grill-me 访谈定下）

1. **产出形态**：一次生成一整套文件（1 个 config.yaml + 每个已确认实体类型 1 个 CSV +
   每个已确认 `(subject_type, relation_type, object_type)` 组合 1 个 CSV），下载时打包
   成一个 zip。不按 `relation_type` 去重合并——一条 `RelationMapping` 只能声明一对固定
   的 subject/object 类型，同一个 relation_type 出现在多个已确认组合里时，每个组合各自
   独立生成一份文件，这是配置格式本身的硬约束，不是风格选择。
2. **文件命名**（生成的文件名必须和 config.yaml 里各 mapping 的 `source_file` 完全对应，
   保证不用打开 YAML 也能看懂文件用途）：
   - 实体类型 CSV：`{term_type}.csv`
   - 关系 CSV：`{subject_term_type}_{relation_type}_{object_term_type}.csv`
   - 两者的唯一性由数据库主键保证（`term_type` 在 `(tenant_id, value, status)` 上唯一，
     三元组在允许列表里唯一），生成时不需要额外去重逻辑；但 `term_type`/`relation_type`
     是用户/系统自由文本，写入文件名前必须做路径安全消毒（不能让它们逃出 zip 的顶层，
     也不能把非法文件名字符直接拼进去）。
3. **预览呈现**：页面内"文件列表 + 点选查看"，默认选中并展开 `config.yaml` 的内容。
   不做逐文件全部平铺展示（文件数量可能很多）。
4. **`node_key_parts` 生成规则**：只使用简单写法 `{column: ...}`，不演示
   `{allocated_code: {...}}` 这种进阶写法——那是给"源数据没有现成唯一编码"这种特殊情况
   用的兜底机制，放进示例反而增加认知负担。
5. **示例行数**：每个文件生成 2 行示例数据（1 行看不出这是表格，3 行以上对纯示意文件
   来说是冗余）。
6. **`field_mappings` 列名与字段名故意不同**：CSV 表头故意不等于本体声明的字段名（比如
   字段名 `价格`、CSV 列名生成为 `价格列`），用来直观体现"这两者是两件独立的事，不需要
   写成一样"这个本功能最容易被用户误解的点。
7. **确认门槛**：要求本体 schema 已确认（复用 `is_ontology_confirmed`，跟真正跑批用的
   门槛一致）才能生成。未确认时禁用/提示；已确认但一个实体类型都没有（空 schema）时不
   生成任何文件，提示"当前租户还没有任何已确认的实体类型，无法生成示例"。已确认且有实体
   类型、但没有任何关系（或反过来）属于正常输出，不特殊报错。
8. **完全无状态**：生成动作不写任何数据库表（不进 `etl_runs_store`，不占用"历史跑批"
   位置，不需要 `run_id`/状态机）。新增的是纯函数式的只读生成逻辑 + 两个只读 GET 端点。
9. **UI 位置**：放在现有上传表单**上方**，做成一个默认**折叠**的区块（标题栏"查看示例
   数据"，带一句引导文案），点开后左侧文件列表（默认选中 `config.yaml`），右侧/下方是
   选中文件的内容预览，底部"下载全部（zip）"按钮。

## 生成规则细节

对每个已确认的 `term_type`（`app/graphrag/ontology_categories.py::TermTypeCategory`）：

- 文件名：`{sanitize(term_type)}.csv`
- `node_key` 列名：`{term_type}编号`，示例值 `{term_type}001` / `{term_type}002`
- `standard_name` 列名：`{term_type}名称`，示例值 `示例{term_type}1` / `示例{term_type}2`
- 每个 `extra_fields[i]`（`ExtraFieldSpec{name, value_type}`）：
  - CSV 源列名：`{name}列`
  - `field_mappings` 里记一条 `{name: "{name}列"}`
  - 示例值按 `value_type` 生成（对齐 `schema_etl_row_processing.py::convert_field_value`
    的反向解析规则）：
    - `string` → `示例文本1` / `示例文本2`
    - `number` → `1.5` / `2.5`
    - `integer` → `1` / `2`
    - `number[]` → `1.5;2.5` / `3.5;4.5`（分号分隔，对齐
      `convert_field_value` 里 `raw_value.split(";")` 的解析规则）

对每个已确认的允许组合 `(subject_term_type, relation_type, object_term_type)`
（`app/graphrag/ontology_constraints.py::list_allowed_combinations`）：

- 文件名：`{sanitize(subject_term_type)}_{relation_type}_{sanitize(object_term_type)}.csv`
  （`relation_type` 本身已经是 `^[A-Z][A-Z0-9_]{0,63}$`，天然文件名安全，不需要消毒）
- 该文件需要同时包含 subject 和 object 各自 `node_key_parts` 用到的列——因为
  `_write_relation_mapping` 是拿同一行数据分别按 subject/object 各自的
  `node_key_parts` 去算 `node_key`（`schema_etl.py:170-177`）。由于本方案只生成简单
  `{column: ...}` 形式的 `node_key_parts`，这两列就是 `{subject_term_type}编号` 和
  `{object_term_type}编号`（如果 subject/object 是同一个类型，两列同名——直接复用同一列
  即可，不需要特殊处理）。
- 示例值直接复用该类型 CSV 里两行的 `node_key`：第 1 行 `{subject_type}001` /
  `{object_type}001`，第 2 行 `{subject_type}002` / `{object_type}002`。

`config.yaml`：

```yaml
tenant_id: {tenant_id}

entities:
  - term_type: {term_type}
    source_file: {term_type}.csv
    standard_name_column: {term_type}名称
    node_key_parts:
      - column: {term_type}编号
    field_mappings:
      {field_name}: {field_name}列
      ...
  ...

relations:
  - relation_type: {relation_type}
    source_file: {subject}_{relation_type}_{object}.csv
    subject_term_type: {subject}
    object_term_type: {object}
  ...
```

（没有任何已确认关系组合时，`relations:` 段生成为空列表 `[]`，不省略这个键——
`load_schema_etl_config` 用 `data.get("relations") or []` 兜底，省略也能解析，但显式
写出空列表能让用户看清楚"这一段本来就该长这样"。）

## 接口设计

- `GET /api/admin/{tenant_id}/schema-etl/sample` → JSON
  `{"files": [{"filename": str, "content": str}, ...]}`，`files[0]` 固定是
  `config.yaml`（供前端默认选中展示）。用于页面内预览，不落盘、不经过 zip。
- `GET /api/admin/{tenant_id}/schema-etl/sample.zip` → `application/zip` 流式响应，
  `Content-Disposition: attachment; filename="{tenant_id}_schema_etl_sample.zip"`。
  用于"下载全部"按钮。

两个端点共用同一个纯函数生成核心（`app/graphrag/schema_etl_sample.py`），只是序列化
方式不同，避免生成逻辑重复维护两份。

两个端点都先检查 `is_ontology_confirmed`：`False` → `400`，detail 说明"本体 schema
还没有确认"。生成核心内部对"没有任何已确认实体类型"这个情况抛一个新的
`EmptySchemaError`，路由层捕获后同样返回 `400`，detail 是"当前租户还没有任何已确认的
实体类型，无法生成示例"。

## 前端设计

`SchemaEtlPage.tsx` 现有上传表单上方新增一个可折叠区块：

- 折叠态：一行标题"查看示例数据"+ 展开箭头，默认折叠。
- 展开时（首次展开才发请求，之后缓存在 state 里不重复请求）：调用
  `GET .../schema-etl/sample`。
  - 若返回 400（未确认 / 空 schema），展示对应的错误文案，不渲染文件浏览器。
  - 成功则渲染：左侧文件名列表（默认选中 `files[0]`，即 `config.yaml`），右侧
    `<pre>` 块展示选中文件的 `content`（纯文本展示，YAML/CSV 都不做语法高亮，跟页面
    现有其它预览区块的朴素风格一致）。
  - 底部"下载全部（zip）"按钮：调用 `GET .../schema-etl/sample.zip`，走
    `handleDownloadReport` 已经用过的 blob→临时 `<a>` 下载模式。

## Global Constraints

- 本体查询一律只读 `status="confirmed"`，复用 `list_term_types`/
  `list_allowed_combinations` 现有函数，不新增查询路径。
- 生成核心是纯函数（给定已确认的 term_types + allowed_combinations，返回
  `{filename: content}` 的有序字典），不接收数据库连接以外的隐式状态，方便单元测试
  直接喂造好的 `TermTypeCategory`/组合列表，不需要整套 sqlite fixture。
- 不写任何数据库表，不引入 `run_id`/状态机。
- 文件名消毒复用 `app/api/admin_schema_etl_routes.py::_UNSAFE_NAME_CHARS` 同款正则
  （或抽出一个共享工具函数），防止 term_type 里的路径分隔符等字符逃出 zip 顶层。
