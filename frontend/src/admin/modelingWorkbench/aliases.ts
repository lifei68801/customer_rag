/**
 * 别名/列名归一化：去掉空白、下划线、连字符，再转小写。
 *
 * 必须跟后端 app/graphrag/ontology_skills.py::normalize_alias 逐字对应。
 * 两边不一致的后果不是报错，而是"前端说这列对上了、后端导出的别名对不上"
 * 这种谁也解释不了的错位，所以 aliases.test.ts 用的是跟后端那份同一批输入。
 *
 * 只做无损折叠，不做同义词、不做前缀匹配：猜错列会把错误数据写进图谱，
 * 比让用户手动指一次列贵得多。
 */
export function normalizeAlias(text: string): string {
  return text.replace(/[\s_-]+/g, '').toLowerCase()
}
