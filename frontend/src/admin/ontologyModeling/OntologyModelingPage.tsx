import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { DEFAULT_MODELING_WAY, PAGE_TITLES, type ModelingWay } from '../../adminRoutes'
import { OntologySchemaPage } from '../OntologySchemaPage'
import { ModelingWorkbenchPage } from '../modelingWorkbench/ModelingWorkbenchPage'
import { SmartCreatePanel } from './smart/SmartCreatePanel'

const WAYS: { id: ModelingWay; label: string; hint: string }[] = [
  { id: 'template', label: '模板构建', hint: '从内置领域模板起步，对齐数据表，看过差异再写入草稿' },
  { id: 'manual', label: '手动构建', hint: '逐条维护实体类型、关系类型和约束' },
  { id: 'smart', label: '智能创建', hint: '回答几个问题，让模型先猜一版骨架，再用业务问题校准' },
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
              onClick={() => switchTo(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-sm text-ink-soft">{WAYS.find((w) => w.id === way)!.hint}</p>
      </div>

      {way === 'template' && <ModelingWorkbenchPage />}
      {way === 'manual' && <OntologySchemaPage />}
      {way === 'smart' && <SmartCreatePanel />}
    </div>
  )
}
