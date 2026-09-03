import { useState, type ChangeEvent } from 'react'
import { PAGE_TITLES } from '../../adminRoutes'
import { useAdminAuth } from '../useAdminAuth'
import { scanTableFile } from './columnStats'
import { assignRoles } from './columnRoles'
import { initialDecision } from './draftProposal'
import type { GuidedDecision, RoledColumn } from './types'

type Step = 'upload' | 'scanning' | 'review' | 'submitting'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

/**
 * 引导式本体建模的入口页。
 *
 * 第一步只做一件事：拿一张表，扫描出列统计，判定每列的角色，产出一份
 * 初始决策，进入第二步的复核。扫描一张大表要几秒——什么都不显示的话
 * 用户会以为页面卡了，然后重复点击或刷新，所以扫描中必须显示进度。
 * 扫描失败（比如 xlsx 超过体积上限）也必须说清原因，不能静静停住。
 */
export function GuidedOntologyPage() {
  const { role } = useAdminAuth()
  const [step, setStep] = useState<Step>('upload')
  const [error, setError] = useState<string | null>(null)
  const [roled, setRoled] = useState<RoledColumn[] | null>(null)
  const [decision, setDecision] = useState<GuidedDecision | null>(null)

  if (role !== 'admin') {
    return (
      <div data-testid="no-permission" className="flex flex-col gap-2">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.guidedOntology}</h1>
        <p className="text-sm text-ink-soft">
          这个页面只有管理员能用。需要建模，请联系管理员。
        </p>
      </div>
    )
  }

  const handleFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    setError(null)
    setStep('scanning')
    // 不人为延迟：scanTableFile 内部按 1MB 分块 await，大表天然会让出主线程画出进度。
    try {
      const stats = await scanTableFile(file)
      const roledColumns = assignRoles(stats)
      const initial = initialDecision(roledColumns)
      setRoled(roledColumns)
      setDecision(initial)
      setStep('review')
    } catch (err) {
      // 静静停住的话用户只会以为自己点漏了，反复重传同一张（超限的）表。
      setError(err instanceof Error ? err.message : '扫描失败，原因未知')
      setStep('upload')
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.guidedOntology}</h1>
        <p className="text-sm text-ink-soft">
          传一张表，先扫描出每一列的统计量，再据此判定角色，帮你搭出第一版本体草案。
        </p>
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-card border border-status-error bg-card px-3 py-2 text-sm text-ink"
        >
          {error}
        </p>
      )}

      {step === 'upload' && (
        <label className="flex max-w-md flex-col gap-1 text-sm font-bold text-ink">
          选择一张表
          <input
            type="file"
            accept=".csv,.tsv,.xlsx,.xls"
            onChange={(event) => void handleFileChange(event)}
            className={`rounded-control border border-subtle bg-paper px-3 py-2 text-ink ${focusRing}`}
          />
        </label>
      )}

      {step === 'scanning' && (
        <p className="text-sm text-ink-soft" aria-live="polite">
          正在扫描……这一步要读完整张表，大表可能需要几秒。
        </p>
      )}

      {step === 'review' && roled && decision && (
        <p className="text-sm text-ink-soft">
          扫描完成，共 {roled.length} 列。下一步的复核界面还在建。
        </p>
      )}
    </div>
  )
}
