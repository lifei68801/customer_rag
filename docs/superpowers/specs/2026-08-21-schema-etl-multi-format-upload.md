# Schema ETL 上传支持多格式数据文件 —— 设计决策记录

**日期**：2026-08-21
**背景**：「表格数据 ETL」上传功能目前只支持 CSV，用户要求加上 XLSX 及其他常见表格格式的支持。

## 现状盘点

功能入口：`DataEntryPage.tsx`「表格导入」tab → `SchemaEtlPage.tsx`（内嵌向导 + 直接上传表单）→ `SchemaEtlConfigBuilder.tsx`（向导式配置构建器）。上传接口 `POST /api/admin/{tenant_id}/schema-etl/runs`（multipart，`config` + 多个 `data_files`）。

- 前端两处 `accept=".csv"` 写死（`SchemaEtlConfigBuilder.tsx:110`、`SchemaEtlPage.tsx:442`）。
- 前端唯一解析文件内容的地方是 `csvHeader.ts`：把文件当纯文本读，手写 RFC4180 逗号+引号解析表头，只为了在"添加数据文件"步骤把列名读出来展示给用户做映射。
- 后端上传 endpoint（`admin_schema_etl_routes.py`）完全不校验文件类型，原始字节直接落盘。
- 真正解析发生在异步任务 `app/graphrag/schema_etl.py` 的 `_read_csv_rows`：用标准库 `csv.DictReader`，硬编码 `encoding="utf-8"`，逐行产出 `dict[str, str]`，被 `_write_entity_mapping`/`_write_relation_mapping` 调用。
- `EntityMapping`/`RelationMapping`/`compute_node_key`/`convert_field_value` 这一整套映射/转换逻辑操作的都是"一行的列名→字符串值"这个格式无关的抽象，不需要改动。
- 依赖：后端目前没有 `pandas`/`openpyxl`/`xlrd`；前端没有任何 XLSX 解析库。

## 决策 1：支持的格式

除 CSV 外，新增：
- **XLSX**（现代 Excel，2007+，zip+XML 格式，`openpyxl` 直接流式读取）
- **XLS**（旧版 Excel 97-2003，二进制格式，`openpyxl` 不支持，需要 `xlrd`；`xlrd` 新版已放弃对 xlsx 的支持，只专注 xls，正好对应这里的用途）
- **TSV**（制表符分隔，本质是 CSV 的变体，只是分隔符从逗号换成制表符，`csv.DictReader` 传 `delimiter="\t"` 即可支持，成本极低）

不做的：JSON、ODS 等其他格式，本次不在范围内，以后有需要再加。

## 决策 2：多 Sheet 处理

固定读取 Excel 文件的**第一个工作表**，其余 sheet 忽略，不提示、不报警告。理由：绝大多数业务数据导出场景（ERP/进销存系统导出的 SKU 表这类）本来就只有一个 sheet，多 sheet 是少数情况；先用最简单的实现覆盖主流场景，不引入 sheet 选择器这类 UI 改动。

## 决策 3：单元格类型 → 字符串转换规则

Excel 单元格的原生类型（`openpyxl`/`xlrd` 读出来的 Python 类型：`int`/`float`/`datetime.datetime`/`datetime.date`/`bool`/`None`/`str`）统一转换成字符串，规则：

| 单元格类型 | 转换规则 | 示例 |
|---|---|---|
| `int` | `str(value)` | `123` → `"123"` |
| `float` 且值等于其整数部分 | 转成 `int` 再 `str()`，去掉尾随 `.0` | `123.0` → `"123"` |
| `float` 且有小数部分 | `str(value)`，不额外补零/截断 | `123.45` → `"123.45"` |
| `datetime.datetime`（有时间部分非 00:00:00，或单元格本身是 datetime 类型） | `strftime("%Y-%m-%d %H:%M:%S")` | `2026-08-21 14:30:00` → `"2026-08-21 14:30:00"` |
| `datetime.date` 或时间部分是 00:00:00 的 `datetime.datetime`（纯日期单元格） | `strftime("%Y-%m-%d")` | `2026-08-21` → `"2026-08-21"` |
| `bool` | `str(value)`（Python 原生 `True`/`False`） | `True` → `"True"` |
| `None` / 空单元格 | 空字符串 `""` | — |
| `str` | 原样返回（可以 `.strip()` 掉首尾空白，跟 CSV 场景一致） | — |

这个转换在读取器内部完成，对下游 `convert_field_value`（按 schema 声明的 `value_type` 再次转换）完全透明——下游收到的永远是 `dict[str, str]`，跟 CSV 路径完全一致的契约。

## 决策 4：后端扩展名白名单校验

上传 endpoint（`start_schema_etl_run`）新增扩展名白名单校验：只接受 `.csv`/`.tsv`/`.xlsx`/`.xls`（大小写不敏感），不在白名单里的文件在写入磁盘之前直接返回 `400 Bad Request`，错误信息里带上具体是哪个文件、支持哪些类型。校验发生在文件写盘之前，不留下垃圾文件。

`config`（YAML 配置文件）字段的校验逻辑不变，本次不涉及。

## 决策 5：前端 XLSX/XLS 解析库

用 SheetJS 的 `xlsx` 包。同时支持 xlsx 和 xls 两种格式，一个库覆盖两个需求；本次只用到读表头这一个最基础的能力（不涉及写 Excel、不涉及复杂公式/样式）。

前端只需要读文件的**第一行**（表头），不需要把整个文件解析进内存——`XLSX.read()` 配合 `sheetRows: 1` 选项可以做到只解析表头所在的那一小段。

**依赖来源（Task 6 实施后修订，非本决策原始内容）**：最初按本决策的设想从 npm 装（`^0.18.5`），但装完后 `npm audit` 报出该版本带 2 个 high severity CVE（Prototype Pollution [GHSA-4r6h-8v6p-xvw6](https://github.com/advisories/GHSA-4r6h-8v6p-xvw6)、ReDoS [GHSA-5pgg-2g8v-p4x9](https://github.com/advisories/GHSA-5pgg-2g8v-p4x9)），npm 标注"No fix available"——SheetJS 项目已停止向 npm 发布修复版本，只往自己的 CDN（`cdn.sheetjs.com`）发布新版本。Task 6 任务审查发现后，独立验证了 CDN 上的 `0.20.3`（`npm audit` 干净、零运行时依赖），改成从 SheetJS 官方 CDN 装版本锁定的 tarball：

```json
"xlsx": "https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz"
```

这个改动因为把依赖来源指向了 npm 官方注册表之外的第三方 CDN，触发了平台安全监控警告（供应链相关的敏感操作），控制者就此单独征求过人类用户（flyli6880@gmail.com）的明确授权，用户选择保留 CDN 定版本引用，2026-08-22 拍板确认。

## 决策 6：CSV 编码探测（顺带修复的预先存在问题）

后端读 CSV/TSV 时不再硬编码 `UTF-8`：先尝试 UTF-8 解码，如果 `UnicodeDecodeError`，回退尝试 GBK（国内 Excel 导出 CSV 最常见的默认编码）。仍然解码失败则把原始异常网上抛，不做进一步猜测。这个探测策略只覆盖 CSV/TSV 这两种文本格式，Excel 格式（xlsx/xls）本身是二进制容器，读出来的是已解码的 Unicode 字符串，不存在这个问题。

## 影响范围 / 不需要改动的部分

- `EntityMapping`/`RelationMapping` 配置结构（YAML schema）：不改。
- `compute_node_key`/`convert_field_value`（`schema_etl_row_processing.py`）：不改，输入契约仍然是 `dict[str, str]`。
- `EntityMappingEditor.tsx`/`RelationMappingEditor.tsx`：不改，消费的是格式无关的 `columns: string[]`。
- Schema ETL 的 YAML 配置本身（`.yaml`/`.yml` 上传）：不改，这次只影响 `data_files` 这一类。

## 验证方式

- 后端：`tests/graphrag/test_schema_etl.py` 新增 xlsx/xls/tsv 源文件的实体/关系写入用例；`tests/graphrag/test_schema_etl_row_processing.py` 或新建一个测试文件覆盖单元格类型转换规则（决策 3 的表格逐条断言）；`tests/api/test_admin_schema_etl_routes.py` 新增扩展名白名单拒绝的用例（决策 4）；新增 GBK 编码 CSV 探测成功的用例（决策 6）。跑 `pytest`。
- 前端：项目目前没有测试框架覆盖这个目录（`csvHeader.ts` 之前也没测试），保持现状，不强制补齐（如果任务执行时判断成本低可以顺手加，不强制）。用 `npx tsc --noEmit` 做类型检查。
- 手工验证：本 session 没有浏览器自动化工具，验证步骤写成"预期结果描述"——上传一个 xlsx 文件到向导的"添加数据文件"步骤，预期能看到表头列名正确展示在映射选择器里；跑一次完整的 ETL（xlsx 源文件），预期 `report.csv` 里的写入/跳过统计与用等价 CSV 文件跑一遍的结果一致。
