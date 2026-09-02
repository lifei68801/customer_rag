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
}

export type ColumnRole = 'identifier' | 'measure' | 'freetext' | 'date' | 'dimension'

export interface RoledColumn {
  stats: ColumnStats
  role: ColumnRole
  /** 判定依据。必须带具体数字——用户要能据此推翻它。 */
  reason: string
}
