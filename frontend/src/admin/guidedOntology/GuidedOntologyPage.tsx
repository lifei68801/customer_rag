import { useState, type ChangeEvent } from 'react'
import { Link } from 'react-router-dom'
import { ADMIN_ROUTES, PAGE_TITLES } from '../../adminRoutes'
import { adminFetch, extractErrorDetail } from '../adminApi'
import { useAdminAuth } from '../useAdminAuth'
import { useAdminTenant } from '../TenantContext'
import { buildConfigYaml } from '../schemaEtlConfigBuilder/buildConfigYaml'
import { scanTableFile } from './columnStats'
import { assignRoles } from './columnRoles'
import { buildProposal, initialDecision, toEtlBuilder } from './draftProposal'
import { ProposalReview } from './ProposalReview'
import type { GuidedDecision, RoledColumn } from './types'

type Step = 'upload' | 'scanning' | 'review' | 'submitting' | 'done'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

const primaryButtonClass = `min-h-[44px] cursor-pointer self-start rounded-control border border-subtle bg-accent-primary px-4 py-2 text-sm font-bold text-on-accent transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`

const secondaryButtonClass = `inline-flex min-h-[44px] cursor-pointer items-center rounded-control border border-subtle bg-paper px-4 py-2 text-sm font-bold text-ink transition hover:bg-interactive-hover active:scale-95 active:opacity-90 ${focusRing}`

/**
 * 引导流程只处理单表——所以一个固定的 fileId 就够了，不需要按上传次数
 * 生成新 id。ETL 映射下载时用它把实体/关系映射回同一个文件名。
 */
const GUIDED_FILE_ID = 'guided-upload'

/**
 * 引导式本体建模的入口页。
 *
 * 第一步只做一件事：拿一张表，扫描出列统计，判定每列的角色，产出一份
 * 初始决策，进入第二步的复核。扫描一张大表要几秒——什么都不显示的话
 * 用户会以为页面卡了，然后重复点击或刷新，所以扫描中必须显示进度。
 * 扫描失败（比如 xlsx 超过体积上限）也必须说清原因，不能静静停住。
 */
export function GuidedOntologyPage() {
  const { role, sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const [step, setStep] = useState<Step>('upload')
  const [error, setError] = useState<string | null>(null)
  const [roled, setRoled] = useState<RoledColumn[] | null>(null)
  const [decision, setDecision] = useState<GuidedDecision | null>(null)
  const [uploadedFile, setUploadedFile] = useState<File | null>(null)

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
    setUploadedFile(file)
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

  /**
   * 一次请求写入整套本体。逐个 term/relation 分别发请求的话，中途失败会
   * 留下半份草稿，而 checkout_draft **不会**清空它（只在"还没检出过"时
   * 才从已确认版复制）——用户没有干净的重来方式。
   */
  const handleSubmit = async () => {
    if (!sessionToken || !roled || !decision || step === 'submitting') return
    const proposal = buildProposal(roled, decision)
    if (proposal.termTypes.length === 0) {
      // 没有标识列、又把猜测根和其余维度列都改判成属性时，proposal 里一个
      // 实体都没有。POST 一份空本体没有意义——挡在这里，说清原因，比让
      // 后端 400（或者更糟，接受一份空草稿）都更早、更直接地告诉用户。
      setError('这份草案里一个实体都没有，没法写入草稿。回上面把至少一列改回「建成实体」。')
      return
    }
    setStep('submitting')
    setError(null)
    try {
      const response = await adminFetch(
        `/api/admin/ontology/${encodeURIComponent(tenantId)}/draft/replace`,
        sessionToken,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            term_types: proposal.termTypes,
            relation_types: proposal.relationTypes,
            constraints: proposal.constraints,
          }),
        },
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '写入草稿失败'))
      }
      // 刻意**不**调 /confirm：确认是不可逆的（旧的已确认版本会被换掉），
      // 引导不该替用户做这个决定。
      setStep('done')
    } catch (err) {
      // 失败时留在原地：跳走的话用户以为成功了，回头发现草稿是空的。
      setError(err instanceof Error ? err.message : '写入草稿失败')
      setStep('review')
    }
  }

  /**
   * 顺带产出 ETL 映射下载。引导收集的信息已经够生成映射了——让用户在
   * ETL 页把同样的判断（哪列是标识、哪列是属性）再做一遍是重复劳动，
   * 而且两次结果可能不一致，那时以哪个为准？
   */
  const handleDownloadMapping = () => {
    if (!roled || !decision || !uploadedFile) return
    const { entities, relations } = toEtlBuilder(roled, decision, GUIDED_FILE_ID)
    const yaml = buildConfigYaml({
      tenantId,
      entities,
      relations,
      files: [
        {
          id: GUIDED_FILE_ID,
          file: uploadedFile,
          columns: roled.map((c) => c.stats.name),
        },
      ],
    })
    const blob = new Blob([yaml], { type: 'text/yaml;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${tenantId}-etl-config.yaml`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
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
          data-testid="page-error"
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

      {(step === 'review' || step === 'submitting') && roled && decision && (
        <div className="flex flex-col gap-6">
          <p className="text-sm text-ink-soft">扫描完成，共 {roled.length} 列。</p>
          <ProposalReview
            roled={roled}
            decision={decision}
            onDecisionChange={setDecision}
            proposal={buildProposal(roled, decision)}
          />
          <button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={step === 'submitting'}
            className={primaryButtonClass}
          >
            {step === 'submitting' ? '写入中…' : '写入草稿'}
          </button>
        </div>
      )}

      {step === 'done' && (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-ink">
            本体草稿已写入。它还没有生效——去「本体结构」页核对一遍再确认。
            确认是不可逆的：旧的已确认版本会被换掉。
          </p>
          <div className="flex flex-wrap gap-2">
            <Link to={ADMIN_ROUTES.ontology} className={secondaryButtonClass}>
              本体结构
            </Link>
            {/* 引导收集的信息已经够生成映射了，不用在 ETL 页重配一遍。 */}
            <button type="button" onClick={handleDownloadMapping} className={secondaryButtonClass}>
              下载 ETL 映射配置
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
