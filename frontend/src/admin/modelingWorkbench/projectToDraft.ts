import { buildConfigYaml } from '../schemaEtlConfigBuilder/buildConfigYaml'
import type { AddedFile, BuilderEntity, BuilderRelation } from '../schemaEtlConfigBuilder/types'
import { parseOptionsOf } from './types'
import type { DraftPayload, SkippedRelation, WorkspaceState, WorkspaceTermType } from './types'

/**
 * 工作区 → `/draft/replace` 的 payload。
 *
 * 实体/关系只取 review === 'accepted' 的：pending 是"还没看"，rejected 是"看过
 * 不要"，两者都不该进本体。拒掉的元素**留在工作区里**（spec 决策 2），这里只
 * 是不投影。
 *
 * 约束不单独审阅：骨架面板没有约束区块，它的去留由它引用的主语/宾语/关系
 * 三个元素的审阅决定——三个都 accepted 就进，任一个没进本体就整条丢掉。
 * 只排除 review === 'rejected'（v2 单独拒绝一条约束用），pending 照常投影；
 * 要是也要求约束 accepted，主路径产出的草稿永远没有约束，而且每次应用都会
 * 把「本体结构」页已有的约束删光。
 *
 * 引用过滤而不是原样带过去：replace_draft 会对引用未声明类型的约束抛
 * UnknownCategoryError，整次应用失败，而用户看到的只是一句"引用了未声明的
 * 实体类型"——他并不知道是自己哪一次拒绝造成的。
 */
export function projectToDraftPayload(state: WorkspaceState): DraftPayload {
  const terms = state.term_types.filter((t) => t.review === 'accepted')
  const relations = state.relation_types.filter((r) => r.review === 'accepted')
  const termValues = new Set(terms.map((t) => t.value))
  const relationNames = new Set(relations.map((r) => r.relation_type))
  return {
    term_types: terms.map((t) => ({
      value: t.value,
      extra_fields: t.extra_fields.map((f) => ({ ...f })),
      standard_name_value_type: t.standard_name_value_type,
    })),
    relation_types: relations.map((r) => ({
      relation_type: r.relation_type,
      example_phrase: r.example_phrase,
      description: r.description,
      allow_chain_query: true,
    })),
    constraints: state.constraints
      .filter(
        (c) =>
          c.review !== 'rejected' &&
          termValues.has(c.subject) &&
          termValues.has(c.object) &&
          relationNames.has(c.relation),
      )
      .map((c) => ({
        subject_term_type: c.subject,
        relation_type: c.relation,
        object_term_type: c.object,
      })),
  }
}

/**
 * 工作区 → ETL 映射 YAML。没有任何实体接上了数据时返回 null——那时这次应用
 * 不带映射，replace_draft 会保留已有的那份（见它的 docstring）。
 *
 * 复用 buildConfigYaml 而不是自己拼 YAML：它已经处理了引号转义、sources 段、
 * allocated_code 这些细节，而且表格导入页用的就是它，两条路径产出的 YAML
 * 形状一致，后端只需要认一种。
 *
 * 它要一个 AddedFile（含真正的 File 对象）才能拿到文件名。工作台这里只有
 * 文件名——用户可能是上一次会话传的表，File 对象早没了。所以构造一个空的
 * `new File([], name)`：buildConfigYaml 只读 `file.name`，不读内容。
 */
export function projectToEtlYaml(
  state: WorkspaceState,
  tenantId: string,
): { yaml: string; fileName: string; skippedRelations: SkippedRelation[] } | null {
  const matched = state.term_types.filter((t) => t.review === 'accepted' && t.data_match !== null)
  if (matched.length === 0) return null

  const fileNames = [...new Set(matched.map((t) => t.data_match!.source_file))]
  const files: AddedFile[] = fileNames.map((name) => ({
    id: name,
    file: new File([], name),
    columns: [],
    parseOptions: parseOptionsOf(state.sources.find((s) => s.file === name)),
  }))

  const entities: BuilderEntity[] = matched.map((term) => {
    const match = term.data_match!
    return {
      id: term.value,
      termType: term.value,
      fileId: match.source_file,
      // 标准名暂用键列：工作台没有"显示名取哪一列"的判断依据，而键列一定
      // 存在。用户可以在表格导入页改。
      standardNameColumn: match.key_columns[0],
      nodeKeyParts: match.key_columns.map((column) => ({ kind: 'column' as const, column })),
      fieldMappings: { ...match.field_columns },
    }
  })

  const { relations, skipped } = pickRelations(state, matched)

  return {
    yaml: buildConfigYaml({ tenantId, entities, relations, files }),
    fileName: fileNames[0],
    skippedRelations: skipped,
  }
}

/**
 * 决定哪些约束能出关系映射。
 *
 * 既有 ETL 的关系语义：从主语表出，**同一行**用宾语实体的 node_key 列算宾语键
 * （etl_projection 不接受"宾语键在这张表叫别的名字"）。所以宾语的键列必须原名
 * 出现在主语表里，否则每一行都因缺列变成 RowFailure——整条关系静默跳过，配置层
 * 不报错。这里提前判掉，把原因交给应用面板说出来，比让用户跑完批才发现强。
 *
 * 主语表没有 columns（v1 存下的旧工作区）时视为未知，同样跳过：宁可让用户重扫
 * 一次，也不出一条可能整条跑空的映射。
 *
 * 约束的取舍规则同 projectToDraftPayload：只排除 rejected，pending 照常带，
 * 再看主宾两端是否都接上了数据，最后才是这里新加的列存在性检查。
 */
function pickRelations(
  state: WorkspaceState,
  matched: WorkspaceTermType[],
): { relations: BuilderRelation[]; skipped: SkippedRelation[] } {
  const relations: BuilderRelation[] = []
  const skipped: SkippedRelation[] = []
  for (const c of state.constraints) {
    if (c.review === 'rejected') continue
    const subject = matched.find((t) => t.value === c.subject)
    const object = matched.find((t) => t.value === c.object)
    if (!subject || !object) continue
    const subjectFile = subject.data_match!.source_file
    const columns = state.sources.find((s) => s.file === subjectFile)?.columns
    const entry = { subject: c.subject, relation: c.relation, object: c.object }
    if (columns === undefined) {
      skipped.push({ ...entry, reason: `主语表 ${subjectFile} 还没重新扫描过，不知道有哪些列` })
      continue
    }
    const names = new Set(columns.map((col) => col.name))
    const missing = object.data_match!.key_columns.filter((k) => !names.has(k))
    if (missing.length > 0) {
      skipped.push({ ...entry, reason: `主语表 ${subjectFile} 里没有 ${c.object} 的键列 ${missing.join('/')}` })
      continue
    }
    relations.push({
      id: `${c.subject}-${c.relation}-${c.object}`,
      // 关系从主语所在的那张表出：那张表的每一行都指向一个宾语。
      fileId: subjectFile,
      subjectTermType: c.subject,
      relationType: c.relation,
      objectTermType: c.object,
    })
  }
  return { relations, skipped }
}

/** 应用面板预览用：跟 projectToEtlYaml 走同一套判断，不用等真正投影才知道哪些出不了。 */
export function previewSkippedRelations(state: WorkspaceState): SkippedRelation[] {
  const matched = state.term_types.filter((t) => t.review === 'accepted' && t.data_match !== null)
  return pickRelations(state, matched).skipped
}
