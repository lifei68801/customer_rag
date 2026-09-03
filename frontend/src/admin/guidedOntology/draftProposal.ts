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
  // 函数的定义域天然是双端的（从 subject 指向 object 的关系），当前实现
  // 只用了 object 是简化版：中文列名暂时产不出比 RELATES_TO 更好的名字。
  // subject 留给将来产出更贴切的关系名（比如「订单号 -> 公司」该产出
  // SOLD_BY 而不是泛泛的 HAS_公司）——调用方（initialDecision 的 root、
  // buildProposal 的 parent）已经天然持有这个值，删掉参数只会让将来这个
  // 改进要重改两处调用签名。
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
 *
 * 注意：第一步把每个非法字符换成下划线，这一步本身不会产出空串——一个
 * 纯中文列名会变成一串下划线（"客户备注" -> "____"），而下划线本身合法，
 * 不会被第二步的"去掉不合法的前导字符"清掉。所以不能只判断
 * `cleaned === ''`：必须判断清洗结果里有没有至少一个字母或数字，没有就
 * 说明这个"合法字符串"其实是空壳——两个不同的中文列名很可能都清成
 * "____"，静默撞成同一个字段名，比空串更隐蔽。
 */
export function sanitizeFieldName(column: string, index: number): string {
  const cleaned = column.replace(/[^a-zA-Z0-9_]/g, '_').replace(/^[^a-zA-Z_]+/, '')
  if (cleaned === '' || !/[a-zA-Z0-9]/.test(cleaned)) return `field_${index + 1}`
  return cleaned.slice(0, 64)
}

/**
 * 这一列要不要建成实体类型。
 *
 * 标识列和维度列共用 decision.dimensionsAsEntity 这一张表——两者的改判语义
 * 完全一样（建成实体 / 做成属性），审阅视图里也用同一组单选渲染。
 *
 * 标识列缺省是 true：initialDecision 会显式写进去，但 buildProposal 也可能
 * 拿到别处构造的 decision（测试、将来的持久化），缺键时按 false 处理会把
 * 标识列静默踢出本体——那是比误判更糟的失败形态。
 */
export function isEntityColumn(column: RoledColumn, decision: GuidedDecision): boolean {
  const decided = decision.dimensionsAsEntity[column.stats.name]
  if (column.role === 'identifier') return decided !== false
  return column.role === 'dimension' && decided === true
}

/**
 * 同一份 extra_fields 里字段名必须互不相同，撞了就加序号后缀。
 *
 * sanitizeFieldName 的出口很窄：非法字符一律换成下划线，纯中文列名退回
 * field_N。两列中文名（`a订单` / `a客户`）会双双清成 `a__`。后端
 * _validate_draft_extra_fields 不查内部重名，两条同名字段原样写进
 * ontology_term_types.extra_fields；更要命的是 ETL 那边
 * `Object.fromEntries` 会把同名映射折叠成一条——后一列的数据永远不会被
 * 加载，而界面上、后端校验里都没有任何异常。
 */
function uniqueFieldName(base: string, used: Set<string>): string {
  if (!used.has(base)) return base
  for (let n = 2; ; n += 1) {
    const suffix = `_${n}`
    // 仍要守住后端的 64 字符上限，所以是截断再拼后缀，不是直接拼。
    const candidate = base.slice(0, 64 - suffix.length) + suffix
    if (!used.has(candidate)) return candidate
  }
}

/** 初始决策用的中心：第一个标识列，没有标识列时退回第一个维度列。 */
function defaultRoot(roled: RoledColumn[]): string {
  const identifier = roled.find((c) => c.role === 'identifier')
  if (identifier) return identifier.stats.name
  // 纯维度表（产品主数据这类）没有标识列。拿第一个维度当根——buildProposal
  // 会据此把 rootIsGuessed 置真，UI 要提示用户确认，否则结构会莫名其妙。
  return roled.find((c) => c.role === 'dimension')?.stats.name ?? ''
}

export function initialDecision(roled: RoledColumn[]): GuidedDecision {
  const root = defaultRoot(roled)
  const dimensionsAsEntity: Record<string, boolean> = {}
  const parentOf: Record<string, string> = {}
  const relationNameOf: Record<string, string> = {}

  for (const column of roled) {
    const name = column.stats.name
    if (column.role === 'identifier') {
      // 标识列默认建成实体（它多半就是中心），但要显式写进决策表里，
      // 审阅视图才有东西可渲染、用户才推翻得了。高基数的整数度量（以分为
      // 单位的金额这类）和一列真订单号在分布上无法区分，判错时只有"看得见
      // 并能改"这一条出路。
      dimensionsAsEntity[name] = true
      // 刻意不给标识列写 parentOf：第二个标识列的边由 buildProposal 统一
      // 改挂到中心下面，并出现在改挂提示里（那条路径有测试守着）。
      continue
    }
    if (column.role !== 'dimension') continue
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
  if (column.role === 'identifier') {
    // 被用户改判成属性的标识列。它落到这里的典型情形正是"其实是以分为
    // 单位的金额"——那时该存成 integer；真的编号（ORD1001、混着字母的
    // SKU）存成 string。用 isWholeNumber 而不是 inferredType：高基数的
    // 无小数数值列在扫描阶段已经被改判成 'string' 了。
    return column.stats.isWholeNumber ? 'integer' : 'string'
  }
  return column.stats.inferredType === 'integer' ? 'integer' : 'number'
}

export function buildProposal(roled: RoledColumn[], decision: GuidedDecision): Proposal {
  // 记的是**位置**，不只是名字：一张表里出现两列同名（从电子表格导出的
  // 宽表里不罕见）时，只按名字判断"这一列是不是已经成了实体"会让第二个
  // 同名列在下面的循环里被 return 掉——既不进任何实体的 extra_fields，也
  // 不进 unusedColumns，凭空消失。那正是下面注释里明说不允许的第三种下场。
  //
  // 重名时只有第一列能成为实体（实体类型名必须唯一），其余同名列照常走
  // 属性 / 未使用这两条出口，用户在界面上能看见它们的去向。
  const entityNames = new Set<string>()
  const entityColumnIndexes = new Set<number>()
  roled.forEach((column, index) => {
    const name = column.stats.name
    if (entityNames.has(name)) return
    if (!isEntityColumn(column, decision)) return
    entityNames.add(name)
    entityColumnIndexes.add(index)
  })

  // 中心实体：列顺序里第一个还留在实体列表里的标识列。标识列被用户改判成
  // 属性（或这张表本来就没有标识列）时，顺延给列顺序里第一个还留在
  // entityNames 里的实体；一个实体都不剩时是空串。
  //
  // 这个值同时是属性的挂载点和关系的中心，而且要显式给到 UI（Proposal
  // .rootName）：UI 若自己用「不在 parentOf 里的实体」反推，在顺延发生
  // 后会反推出 undefined，画出一个不存在的环，而提交出去是零条关系。
  //
  // 属性没处挂（entityNames 空）时，属性列必须落进 unusedColumns。任何
  // 一列最终只能是「进了某个实体的 extra_fields」或「进了 unusedColumns」
  // 这两种下场之一，不允许第三种——第三种就是静默丢列。
  const rootName =
    roled.find((c) => c.role === 'identifier' && entityNames.has(c.stats.name))?.stats.name ??
    roled.find((c) => entityNames.has(c.stats.name))?.stats.name ??
    ''
  const attributeHost = rootName === '' ? undefined : rootName

  // 中心是猜的 = 中心那一列不是标识列。两种来源：这张表本来就没有标识列
  // （纯维度表），或者用户把标识列改判成了属性。后一种以前答错：
  // rootIsGuessed 只看"有没有标识列存在过"，用户把它改判掉之后中心已经是
  // 一个维度列了，界面却照旧不出"中心是猜的"那条提示。
  const rootIsGuessed = !roled.some(
    (c) => c.role === 'identifier' && c.stats.name === rootName,
  )

  const renamedFields: Record<string, string> = {}
  const hostFields: DraftExtraField[] = []
  const unusedColumns: string[] = []
  // 原列名，给审阅视图用：度量列和日期列在界面上此前一处都不出现，判错了
  // 用户也看不见。字段名清洗过之后 extra_fields 里的名字可能跟列名对不上，
  // 所以这里按原列名单独记一份，而不是让 UI 去猜。
  const attributeColumns: string[] = []
  const usedFieldNames = new Set<string>()
  // 撞过名、因此被加了序号后缀的列（原列名）。要在界面上说出来：并排显示
  // 改名前后不足以让用户看出"这两行撞了"，而真正的后果（ETL 少加载一列）
  // 发生在下载 YAML 之后。
  const collidedFields: string[] = []

  roled.forEach((column, index) => {
    const name = column.stats.name
    if (entityColumnIndexes.has(index)) return
    const isAttribute =
      column.role === 'measure' ||
      column.role === 'date' ||
      // 被改判成属性的维度列 / 标识列。标识列走到这里，说明用户在审阅视图
      // 里明确说了"这不是标识"——不能再把它扔进未使用清单，那等于吞掉他的
      // 决定。
      column.role === 'dimension' ||
      column.role === 'identifier'
    if (!isAttribute || !attributeHost) {
      // 自由文本、空列，或者没有任何实体可以挂：不进本体。必须列出来
      // ——不显示等于静默丢弃。
      unusedColumns.push(name)
      return
    }
    const base = sanitizeFieldName(name, index)
    const fieldName = uniqueFieldName(base, usedFieldNames)
    if (fieldName !== base) collidedFields.push(name)
    usedFieldNames.add(fieldName)
    if (fieldName !== name) renamedFields[name] = fieldName
    hostFields.push({ name: fieldName, value_type: measureValueType(column) })
    attributeColumns.push(name)
  })

  const termTypes: DraftTermType[] = [...entityNames].map((value) => ({
    value,
    // 属性只挂在 attributeHost 上。别的实体是维度，它们自己的属性得从
    // 别的表来。
    extra_fields: value === attributeHost ? hostFields : [],
    standard_name_value_type: 'string',
  }))

  const constraints: DraftConstraint[] = []
  const relationTypeByName = new Map<string, DraftRelationType>()

  const reparentedNames: string[] = []

  for (const child of entityNames) {
    if (child === rootName) continue
    const declared = decision.parentOf[child]
    // 上级缺失、指向自己（自环 A-[R]->A 会让图谱查询陷进去）、或者指向一个
    // 已经不在实体列表里的名字：都不能照单全收，但也**不能跳过**。跳过的
    // 后果是这个实体一条边都没有，而 UI 照样画出「X 挂在 Y」——界面显示
    // 连好了，提交出去是孤儿。改挂到中心下面，并把名字收集起来让 UI 明说。
    //
    // 如实记一句没堵住的：两个非中心实体互指的二元环（A 的上级选 B、B 的
    // 上级选 A）没有检测。两边各自看都是 valid（都指向一个存在、不是自己
    // 的实体），constraints 非空所以「零关系」告警也不会触发，但中心会被
    // 晾在一边，图谱里出现一个跟中心不连通的二元环。
    const valid = declared && declared !== child && entityNames.has(declared)
    const parent = valid ? declared : rootName
    if (!valid) reparentedNames.push(child)
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
    attributeColumns,
    renamedFields,
    collidedFields,
    rootIsGuessed,
    rootName,
    reparentedTo: { root: rootName, names: reparentedNames },
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
