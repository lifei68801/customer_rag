export type InferredType = 'number' | 'integer' | 'date' | 'string'

export interface ColumnStats {
  name: string
  /** 非空值的行数。分母用它，不用总行数——90% 为空的列不该被当成低基数。 */
  nonEmptyCount: number
  /** 不同值的个数。封顶后等于 DISTINCT_LIMIT，见 distinctCapped。 */
  distinctCount: number
  /** 是否已封顶。封顶意味着"至少这么多"，不是精确值。 */
  distinctCapped: boolean
  /** 前几个不同值，给用户看。只给数字他判断不了。 */
  samples: string[]
  inferredType: InferredType
  /**
   * 扫描时观察到的事实：这一列的非空值是不是**全部**都是不带小数的数值。
   *
   * 和 `inferredType === 'integer'` 不是一回事，两者必须分开存：inferType
   * 会把 distinct 超过 NUMERIC_IDENTIFIER_THRESHOLD 的无小数数值列改判成
   * `'string'`（那是一个关于"它更可能是标识"的判断），改判之后
   * `inferredType` 就答不出"它原本是不是整数"这个问题了。而现实里的整数
   * 度量（数量、单价、以分为单位的金额）几乎都在 50 个不同值以上，全都
   * 落在被改判的那一半里——只看 inferredType 的话，整数专属的解释文案对
   * 真正需要它的列一句都不会出现。
   */
  isWholeNumber: boolean
}

export type ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'

export interface RoledColumn {
  stats: ColumnStats
  role: ColumnRole
  /** 判定依据。必须带具体数字——用户要能据此推翻它。 */
  reason: string
}

export interface DraftExtraField {
  name: string
  value_type: 'string' | 'number' | 'integer' | 'number[]'
}

export interface DraftTermType {
  value: string
  extra_fields: DraftExtraField[]
  standard_name_value_type: 'string'
}

export interface DraftRelationType {
  relation_type: string
  example_phrase: string
  description: string
  allow_chain_query: true
}

export interface DraftConstraint {
  subject_term_type: string
  relation_type: string
  object_term_type: string
}

export interface GuidedDecision {
  /** 每个维度列：建成实体类型（true）还是做成属性（false）。 */
  dimensionsAsEntity: Record<string, boolean>
  /** 每个非根实体挂在谁下面。键是实体名，值是父实体名。 */
  parentOf: Record<string, string>
  /** 每条边的关系类型名。键是子实体名。 */
  relationNameOf: Record<string, string>
}

export interface Proposal {
  termTypes: DraftTermType[]
  relationTypes: DraftRelationType[]
  constraints: DraftConstraint[]
  /** 没进本体的列。不显示等于静默丢弃。 */
  unusedColumns: string[]
  /** 属性名被清洗过的列：原列名 -> 清洗后的字段名。ETL 映射要用它对回去。 */
  renamedFields: Record<string, string>
  /** 没有标识列，根是猜的。UI 要提示用户确认。 */
  rootIsGuessed: boolean
  /**
   * 中心实体名（所有属性挂在它上面，其余实体默认挂在它下面）。
   *
   * 由 buildProposal 显式产出，**不允许 UI 用「不在 parentOf 里的实体」
   * 反推**：用户把猜测根改判成属性后，那个反推会得到 undefined，于是
   * 每个实体都长出一行「挂在」下拉框、下拉框的值又落回第一个选项，界面
   * 显示出一个根本不存在的环，而提交出去的是零条关系。
   *
   * 一个实体都不剩时是空串——那时本体里没有中心可言。
   */
  rootName: string
  /**
   * 上级无效、被自动改挂到中心下面的实体。
   *
   * 两种来源：原来的上级已经不在实体列表里（用户把它改判成属性了），
   * 或者这个实体从来就没有上级条目（initialDecision 只给维度列写
   * parentOf，第二个标识列不在其中）。两种情况下界面都会照常画出一行
   * 「X 挂在 Y」，而不改挂的话那条边根本不会被提交——界面在说谎。
   */
  reparentedTo: { root: string; names: string[] }
}
