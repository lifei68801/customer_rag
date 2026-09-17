# 建模工作台数据面板补齐：手动指列、关系列检查、空白起步退化路径

日期：2026-09-17。承接 `2026-09-16-ontology-modeling-workbench-design.md`（下称主 spec）v1 交付后整分支终审提出的三个已知限制（ledger 里的 I2 / I4 / I5）。三个决定已由用户拍板（2026-09-17）。

## 背景

v1 工作台把"列对齐到骨架"做成了确定性的别名精确匹配（主 spec 行为规格 §3），并且明说失败的代价是"用户手动指一下列"——但 v1 没有做这个手动入口。终审另发现两处：多表关系映射在宾语键列不在主语表里时 ETL 会整条静默跳过；"空白起步 + 只传表"的退化路径（主 spec 前端一节承诺保留 `draftProposal.ts` 做这个）没接线。修注释时还发现列角色的判定依据 `reason`（主 spec 要求"必须带具体数字，用户要能据此推翻"）在新工作台里没有任何面板渲染。

## 目标

1. 别名命不中时，用户在数据面板能把一列指给已有实体当键或当字段；指完写进别名，下次同一客户的表能自动命中。
2. 骨架面板能直接编辑每个实体的键别名与字段别名。
3. 未接住的列旁显示列角色与判定依据（reason）。
4. 投影 ETL 映射时，只对"主语表里真有宾语键列"的约束出关系映射；出不了的在应用面板逐条说明原因。
5. 空白起步且骨架为空时，扫表后用既有 `buildProposal` 推一套骨架（实体 + 属性 + 关系），来源标 `data`、待审。

## 非目标

- 不做值级 join 分析（主 spec 决策 13 不变）。
- 不给关系映射加"宾语键在这张表叫什么列"的新字段（既有 ETL 关系语义不改）。
- 不做 LLM 猜列（v2）。

## 数据模型改动

只改 `state_json`，后端 `validate_state` 只校验外形、对多出来的键放行，**不改后端**。

`sources[]` 条目加 `columns`：

```jsonc
{
  "file": "CN_001.xls", "sheet": null, "header_row": 6, "first_data_row": 7,
  "columns": [
    { "name": "JAN", "role": "identifier", "reason": "100 个非空值里 100 个不同（100%）", "inferred_type": "string" }
  ]
}
```

- `role` 取 `columnRoles.ts` 的 `ColumnRole`；`reason` 原样存扫描时算出的那句；`inferred_type` 取 `ColumnStats.inferredType`。
- 存下来是为了：未接住列旁能显示角色/依据而不重扫；投影关系映射时能判断"主语表里有没有宾语键列"；手动指字段时能推 `value_type`。
- v1 存下的旧工作区 `sources[]` 没有 `columns`，视为"未知"：关系映射按无法判断处理（跳过并说明"这张表还没重新扫描过"），未接住列不显示依据。

`data_match.matched_by` 新增取值 `manual`（主 spec 已预留）；空白起步推出来的取 `column_role`（主 spec 已预留）。

## 行为规格

### 1. 手动指列（数据面板）

未接住列一节，每列一行：列名、角色标（identifier/dimension）、reason 文字、原有的「提升为实体类型」按钮，以及一个「指给…」下拉 + 按钮。下拉列出所有 `review !== 'rejected'` 的实体，每个实体两项：`当 SKU 的键列`、`当 SKU 的字段`。

- **当键列**：该实体 `data_match` 整体替换为 `{source_file: 这张表, key_columns: [列], field_columns: 原 data_match 若是同一张表则保留否则 {}, matched_by: 'manual'}`；列名追加进 `key_aliases`（已有则不重复）；列从 `unmatched_columns[file]` 移除。
- **当字段**：要求该实体已经在**同一张表**有 `data_match`（否则拒绝并提示"先把 X 的键列指到这张表"）。字段内部名 = `sanitizeFieldName(列名, 序号)`，与该实体已有 `extra_fields` 撞名时加序号后缀；`value_type` 由列的 `inferred_type` 推（`integer`→`integer`、`number`→`number`、`date`→`date`、其余 `string`）；`extra_fields` 追加 `{name, value_type, label: 列名}`；`field_columns[name] = 列名`；`field_aliases[name]` 追加列名；列从 `unmatched_columns[file]` 移除。
- 指完立刻整份存回（与其它编辑一致）。

### 2. 别名编辑（骨架面板）

每个实体行下方：`键别名` 文本框（逗号分隔，显示当前 `key_aliases`），每个 `extra_field` 一个 `字段别名` 文本框。保存按钮把文本按逗号/中文逗号拆开、去空白、去重后整份替换对应别名数组。空文本 = 清空。保存后要重新扫描才会影响对齐（界面上提示这一点）。

### 3. 关系列检查（投影）

`projectToEtlYaml` 对每条候选约束（主/宾都有 `data_match`）：取主语表 `sources[].columns` 的列名集合，宾语 `data_match.key_columns` 全部在其中才出 `relations:` 条目；否则不出，并把 `{subject, relation, object, reason}` 收进返回值的 `skipped_relations`。`reason` 三种：`主语表 F 里没有宾语 O 的键列 K`、`主语表 F 还没重新扫描过，不知道有哪些列`、`主语与宾语在同一张表，关系映射不需要`（同表时主宾键列都在，正常出关系——这条其实不会触发，删掉）。应用面板在 diff 下方列出 `skipped_relations`，每条一句话，末尾固定提示"要在表格导入页手动配"。

### 4. 空白起步退化路径

数据面板扫描时若 `state.term_types.length === 0`（不看 `skill_name`——用 skill 起步但把骨架全删光的人同样需要这条路），不走对齐，改走：`buildProposal(roled, initialDecision(roled))` → 转成工作区元素：

- 每个 `proposal.termTypes[i]` → 实体 `{value, display_name: value, provenance: 'data', review: 'pending', standard_name_value_type: 'string', extra_fields: 原样（含 label）, key_aliases: [value], field_aliases: {每个字段: [label]}, clues: [], data_match: {source_file, key_columns: [value], field_columns: {每个字段: label}, matched_by: 'column_role'}}`。
- `proposal.relationTypes[i]` → 关系 `{…, provenance: 'data', review: 'pending', clues: [], data_match: null}`；`proposal.constraints[i]` → 约束 `{subject, relation, object, provenance: 'data', review: 'pending'}`。
- `unmatched_columns[file] = proposal.unusedColumns`（没被用上的列仍然可提升/可指）。
- `sources[]` 照常记 `columns`。

推出来的元素都是 `pending`，用户仍在骨架面板逐条审。

## 接口

无新增后端端点。

## 决策记录

| # | 决定 | 备选 | 为什么 |
|---|---|---|---|
| 1 | 手动指列 + 别名编辑两者都做 | 只做其一 | 指列解决当下这张表，别名编辑解决长期规则；用户选"两者都做" |
| 2 | 关系映射投影时按列存在性过滤并说明，不加新字段 | 给关系加"宾语键列"编辑 | 不改既有 ETL 关系语义，工作量可控；用户选此项 |
| 3 | 空白起步接上 `buildProposal` | 声明 v2 | 主 spec 本来就承诺保留这条路；用户选接上 |
| 4 | 列角色/reason 存进 `sources[].columns` | 每次重扫 | 未接住列旁显示依据不该要求用户重传文件 |
| 5 | 后端不改 | 给 `validate_state` 加 `columns` 校验 | 后端对 state 只校验外形是主 spec 定下的边界；多出来的键放行本来就是设计 |

## 风险

- `sources[].columns` 让 state 变大（每列一句 reason）。上限：一张 200 列的表约 20KB，可接受；主 spec 挂账的"state 无上限"问题不在本次范围。
- 手动指键会**整体替换** `data_match`，用户可能覆盖掉一个自动命中的键列。界面上按钮文案写清"改为用 X 当键列"。
