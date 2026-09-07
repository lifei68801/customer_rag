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
