import type { BuilderEntity, BuilderRelation } from '../schemaEtlConfigBuilder/types'
import type {
  DraftConstraint,
  DraftExtraField,
  DraftRelationType,
  DraftTermType,
  GuidedDecision,
  Proposal,
  RoledColumn,
} from './types'

/**
 * 关系名建议。必须匹配后端的 ^[A-Z][A-Z0-9_]{0,63}$
 * （app/graphrag/ontology_relations.py:24）——不合规的名字后端会 400，
 * 而用户看不出是哪个字符出的问题。
 *
 * 中文列名产不出有意义的英文名，退回 RELATES_TO；用户可以在界面上改。
 */
export function suggestRelationName(subject: string, object: string): string {
  void subject
  const ascii = object
    .replace(/[^a-zA-Z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .toUpperCase()
  if (ascii && /^[A-Z]/.test(ascii)) return `HAS_${ascii}`.slice(0, 64)
  return 'RELATES_TO'
}

/**
 * 把列名清洗成合法的属性字段名。
 *
 * 必须匹配 ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
 * （app/graphrag/ontology_categories.py:26）。中文列名会被清成空串，那时
 * 退回一个带序号的占位名——丢掉这一列更糟，用户至少能在界面上看到它被
 * 改成了什么。
 */
export function sanitizeFieldName(column: string, index: number): string {
  const cleaned = column.replace(/[^a-zA-Z0-9_]/g, '_').replace(/^[^a-zA-Z_]+/, '')
  if (cleaned === '') return `field_${index + 1}`
  return cleaned.slice(0, 64)
}

function rootOf(roled: RoledColumn[]): { name: string; guessed: boolean } {
  const identifier = roled.find((c) => c.role === 'identifier')
  if (identifier) return { name: identifier.stats.name, guessed: false }
  // 纯维度表（产品主数据这类）没有标识列。拿第一个维度当根，并标记为
  // 猜的——UI 要提示用户确认，否则结构会莫名其妙。
  const firstDimension = roled.find((c) => c.role === 'dimension')
  return { name: firstDimension?.stats.name ?? '', guessed: true }
}

export function initialDecision(roled: RoledColumn[]): GuidedDecision {
  const { name: root } = rootOf(roled)
  const dimensionsAsEntity: Record<string, boolean> = {}
  const parentOf: Record<string, string> = {}
  const relationNameOf: Record<string, string> = {}

  for (const column of roled) {
    if (column.role !== 'dimension') continue
    const name = column.stats.name
    // 默认建成实体：少建是静默错误（那类问题就是答不出来，不报错），
    // 多建看得见（实体列表里就有）。
    dimensionsAsEntity[name] = true
    if (name === root) continue
    // 默认星型：一定连通，不会漏掉任何实体；多一条冗余边是看得见的。
    parentOf[name] = root
    relationNameOf[name] = suggestRelationName(root, name)
  }
  return { dimensionsAsEntity, parentOf, relationNameOf }
}

function measureValueType(column: RoledColumn): DraftExtraField['value_type'] {
  // 日期存成 string：数据模型只有 string/number/integer/number[]，没有
  // 日期类型。这不是疏忽，是必须向用户明说的限制。
  if (column.role === 'date') return 'string'
  if (column.role === 'dimension') return 'string'
  return column.stats.inferredType === 'integer' ? 'integer' : 'number'
}

export function buildProposal(roled: RoledColumn[], decision: GuidedDecision): Proposal {
  const { name: root, guessed: rootIsGuessed } = rootOf(roled)

  const entityNames = new Set<string>()
  for (const column of roled) {
    if (column.role === 'identifier') entityNames.add(column.stats.name)
    if (column.role === 'dimension' && decision.dimensionsAsEntity[column.stats.name]) {
      entityNames.add(column.stats.name)
    }
  }

  // 属性一律挂在根实体上。度量、日期、以及被用户取消选中的维度列，描述的
  // 都是"这一行"，而这一行的身份就是根。
  const renamedFields: Record<string, string> = {}
  const rootFields: DraftExtraField[] = []
  const unusedColumns: string[] = []

  roled.forEach((column, index) => {
    const name = column.stats.name
    if (entityNames.has(name)) return
    const isAttribute =
      column.role === 'measure' ||
      column.role === 'date' ||
      (column.role === 'dimension' && !decision.dimensionsAsEntity[name])
    if (!isAttribute) {
      // 自由文本、空列：不进本体。必须列出来——不显示等于静默丢弃。
      unusedColumns.push(name)
      return
    }
    const fieldName = sanitizeFieldName(name, index)
    if (fieldName !== name) renamedFields[name] = fieldName
    rootFields.push({ name: fieldName, value_type: measureValueType(column) })
  })

  const termTypes: DraftTermType[] = [...entityNames].map((value) => ({
    value,
    // 属性只挂在根上。别的实体是维度，它们自己的属性得从别的表来。
    extra_fields: value === root ? rootFields : [],
    standard_name_value_type: 'string',
  }))

  const constraints: DraftConstraint[] = []
  const relationTypeByName = new Map<string, DraftRelationType>()

  for (const child of entityNames) {
    const parent = decision.parentOf[child]
    // parent === child 会造出自环 A-[R]->A，图谱查询会陷进去。
    if (!parent || parent === child || !entityNames.has(parent)) continue
    const relationType = decision.relationNameOf[child] ?? suggestRelationName(parent, child)
    constraints.push({
      subject_term_type: parent,
      relation_type: relationType,
      object_term_type: child,
    })
    // 去重：SOLD_BY 在 demo 里用了两次（订单->公司、产品->公司），重复
    // 声明会撞主键 (tenant_id, relation_type, status)。
    if (!relationTypeByName.has(relationType)) {
      relationTypeByName.set(relationType, {
        relation_type: relationType,
        example_phrase: `${parent} ${relationType} ${child}`,
        description: '',
        // 不暴露给用户：它是查询层的开关，普通用户没有判断依据。
        allow_chain_query: true,
      })
    }
  }

  return {
    termTypes,
    relationTypes: [...relationTypeByName.values()],
    constraints,
    unusedColumns,
    renamedFields,
    rootIsGuessed,
  }
}

/**
 * 顺带产出 ETL 映射。
 *
 * 引导收集的信息已经够生成映射了——让用户在 ETL 页把同样的判断（哪列是
 * 标识、哪列是属性）再做一遍是重复劳动，而且两次结果可能不一致，那时以
 * 哪个为准？
 */
export function toEtlBuilder(
  roled: RoledColumn[],
  decision: GuidedDecision,
  fileId: string,
): { entities: BuilderEntity[]; relations: BuilderRelation[] } {
  const proposal = buildProposal(roled, decision)
  const columnOfField = new Map(
    Object.entries(proposal.renamedFields).map(([column, field]) => [field, column]),
  )

  const entities: BuilderEntity[] = proposal.termTypes.map((termType) => ({
    id: `guided-${termType.value}`,
    termType: termType.value,
    fileId,
    // 实体名就是那一列本身。node_key 用同一列——引导只处理单表单列的简单
    // 情况，复合键要用户去 ETL 页自己配。
    standardNameColumn: termType.value,
    nodeKeyParts: [{ kind: 'column', column: termType.value }],
    fieldMappings: Object.fromEntries(
      termType.extra_fields.map((field) => [
        field.name,
        columnOfField.get(field.name) ?? field.name,
      ]),
    ),
  }))

  const relations: BuilderRelation[] = proposal.constraints.map((constraint, index) => ({
    id: `guided-rel-${index}`,
    fileId,
    subjectTermType: constraint.subject_term_type,
    relationType: constraint.relation_type,
    objectTermType: constraint.object_term_type,
  }))

  return { entities, relations }
}
