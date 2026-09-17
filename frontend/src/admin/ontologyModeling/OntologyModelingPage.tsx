import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { DEFAULT_MODELING_WAY, PAGE_TITLES, type ModelingWay } from '../../adminRoutes'
import { OntologySchemaPage } from '../OntologySchemaPage'
import { ModelingWorkbenchPage } from '../modelingWorkbench/ModelingWorkbenchPage'
import { SmartCreatePanel } from './smart/SmartCreatePanel'

/**
 * 三种方式只是初始化本体的两条思路（模板构建、智能创建）加一个终点（本体
 * 结构）——顺序即流程：先用某一条思路起个头，再到本体结构里核实、增删改，
 * 最后存草稿或确认（design 增补，决策 13）。
 *
 * `manual` 这个 id 不改，只改 label：它是已经发出去的 `?way=manual` 链接
 * 里的取值，改值会让那些链接失效；"手动构建"这个名字才是要退休的。
 */
const WAYS: { id: ModelingWay; label: string; hint: string }[] = [
  {
    id: 'template',
    label: '模板构建',
    hint: '从内置领域模板起步，对齐数据表，看过差异再写入草稿 → 写入后到「本体结构」核实',
  },
  {
    id: 'smart',
    label: '智能创建',
    hint: '回答几个问题，让模型先猜一版骨架，再用业务问题校准 → 写入后到「本体结构」核实',
  },
  {
    id: 'manual',
    label: '本体结构',
    hint: '核实与修改：逐条增删改实体类型、关系类型和约束，然后存草稿或确认',
  },
]

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const wayButtonClass = (active: boolean) =>
  `min-h-[40px] cursor-pointer rounded-control border border-subtle px-4 text-sm font-bold transition ${focusRing} ${
    active ? 'bg-ink text-paper' : 'bg-paper text-ink hover:bg-interactive-hover'
  }`

function parseWay(raw: string | null): ModelingWay {
  // 未知值一律落回缺省，不报错：URL 是用户能手改的，改错了给他最安全的那页
  return raw === 'template' || raw === 'manual' || raw === 'smart' ? raw : DEFAULT_MODELING_WAY
}

/** 拼"刚写入 N 个实体类型、M 个关系类型、K 条约束"——数量为 0 的那一段不提，
 * 免得写成"刚写入 0 条约束"这种没有信息量的话。 */
function describeApplied(counts: { termTypes: number; relationTypes: number; constraints: number }): string {
  const parts: string[] = []
  if (counts.termTypes > 0) parts.push(`${counts.termTypes} 个实体类型`)
  if (counts.relationTypes > 0) parts.push(`${counts.relationTypes} 个关系类型`)
  if (counts.constraints > 0) parts.push(`${counts.constraints} 条约束`)
  const summary = parts.length > 0 ? `刚写入 ${parts.join('、')}。` : ''
  return `${summary}核实一下，需要的话在这里增删改，再存草稿或确认。`
}

/**
 * 本体建模：三种构建方式的壳。
 *
 * 只做 tab 切换与 URL 同步；三套内容各自独立（spec 决策 2），互相不 import，
 * 写草稿都走各自的路径、终点都是同一份草稿。
 *
 * tab 记在 ?way= 上而不是组件状态：链接能分享，刷新不丢；切 tab 保留别的
 * 查询参数（version、from_question），它们属于各自的 tab，不该被切换动作抹掉。
 */
export function OntologyModelingPage() {
  const [params, setParams] = useSearchParams()
  const way = parseWay(params.get('way'))
  // 写入草稿成功后的提示；只在“自动跳到本体结构”那一刻设置，用户自己
  // 切 tab（哪怕又切回本体结构）都会清掉——提示只对刚才那一次写入有效，
  // 留着会让用户以为下一次逛到这个 tab 也是"刚写入"。
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    // 标题跟三个 tab 共用一份——它是壳页的标题，不属于任何一个 tab；
    // 切 tab 不该改标题（三种方式都在"本体建模"这一页里）。
    document.title = `${PAGE_TITLES.ontologyModeling} · 管理后台`
  }, [])

  const switchTo = (next: ModelingWay) => {
    const updated = new URLSearchParams(params)
    updated.set('way', next)
    setParams(updated, { replace: true })
  }

  // 用户手动点 tab：清掉上一次写入留下的提示，它已经跟当前这次浏览无关。
  const handleTabClick = (id: ModelingWay) => {
    setNotice(null)
    switchTo(id)
  }

  // 模板构建 / 智能创建写入草稿成功后调用：自动跳到本体结构并说清刚写入
  // 了什么，不然用户停在生成器里不知道下一步该去哪（design 增补，决策 11）。
  const handleApplied = (counts: { termTypes: number; relationTypes: number; constraints: number }) => {
    switchTo('manual')
    setNotice(describeApplied(counts))
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h1 className="font-mono text-xl font-semibold text-ink">本体建模</h1>
        <div role="group" aria-label="构建方式" className="flex flex-wrap gap-2">
          {WAYS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={way === item.id}
              title={item.hint}
              className={wayButtonClass(way === item.id)}
              onClick={() => handleTabClick(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-sm text-ink-soft">{WAYS.find((w) => w.id === way)!.hint}</p>
      </div>

      {notice && (
        <div role="status" className="flex flex-wrap items-center gap-3 rounded-card border border-subtle bg-card px-3 py-2 text-sm text-ink">
          <span>{notice}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className={`min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover ${focusRing}`}
          >
            知道了
          </button>
        </div>
      )}

      {way === 'template' && <ModelingWorkbenchPage onApplied={handleApplied} />}
      {way === 'manual' && <OntologySchemaPage />}
      {way === 'smart' && <SmartCreatePanel onApplied={handleApplied} />}
    </div>
  )
}
