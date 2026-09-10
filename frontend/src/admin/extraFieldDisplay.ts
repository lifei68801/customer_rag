/**
 * 属性字段的显示名。
 *
 * 一个属性有两个名字：内部名（`name`）会被拼进 Cypher、成为 Neo4j 索引的
 * 属性名和结构化查询接受的字段名，所以只能是 ASCII 标识符；显示名
 * （`label`）只给人看，可以是中文。客户（MUJI 商品知识中台）的字段设计表
 * 本来就是「字段中文名」和「字段ID」两列并存——中文名「当前售价」对应 ID
 * md_sku_price，所以这个拆分是这类项目实际的工作方式。
 *
 * 存量字段没有显示名（后端不做数据迁移，读出来是空串），此时回退到内部名。
 */

export interface DisplayableField {
  name: string
  label?: string
}

export function fieldDisplayName(field: DisplayableField): string {
  // trim 而不是只判空串：全是空格的显示名在界面上是一段看不见的空档，
  // 那一栏会变成空白，用户认不出这是哪个字段。
  return field.label?.trim() || field.name
}

/**
 * 取值类型在界面上的说法。
 *
 * 存储的枚举值不动（改一次要迁移所有租户的存量声明），只改展示。用户问过
 * "属性没有 float 类型，无法用于售价和收入"——float 一直都在，它叫
 * number：ETL 写入时走的是 Python 的 float()（见
 * schema_etl_row_processing.py::convert_field_value），Neo4j 侧对应
 * toFloat。看不出 number 是双精度浮点，等于没有这个类型。
 *
 * 两套说法：下拉选项里带例子（选之前要判断得出该选哪个），行内提示只给
 * 短词（每个属性输入框旁边挂一整句会把表单淹掉）。短词是长句的前半段，
 * 用的是同一个词汇。
 */
const VALUE_TYPE_SHORT_LABELS: Record<string, string> = {
  string: '文本',
  number: '小数',
  integer: '整数',
  'number[]': '小数列表',
  date: '日期',
}

const VALUE_TYPE_OPTION_LABELS: Record<string, string> = {
  string: '文本',
  number: '小数（如 19.99，售价/金额）',
  integer: '整数',
  'number[]': '小数列表',
  date: '日期（如 2026-01-05，可以按「上个月」这类时间范围过滤）',
}

/** 行内提示用的短说法。认不出的枚举值原样返回——显示成空白更糟。 */
export function valueTypeLabel(valueType: string): string {
  return VALUE_TYPE_SHORT_LABELS[valueType] ?? valueType
}

/** 下拉选项用的说法，带例子。 */
export function valueTypeOptionLabel(valueType: string): string {
  return VALUE_TYPE_OPTION_LABELS[valueType] ?? valueType
}
