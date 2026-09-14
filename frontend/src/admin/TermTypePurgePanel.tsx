import { useEffect, useState } from 'react'
import {
  fetchStoredTermTypes,
  previewPurgeTermType,
  purgeTermType,
  PurgeCountChangedError,
  type PurgePreview,
  type StoredTermType,
} from './termsApi'

const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

interface TermTypePurgePanelProps {
  sessionToken: string
  tenantId: string
  /** 清空成功后通知页面刷新列表和摘要。 */
  onPurged: (message: string) => void
}

/**
 * 按实体类型彻底清空——实体明细页底部的"危险操作"区。
 *
 * ## 为什么在这一页、为什么折叠
 *
 * 用户是在这一页批量删除之后发现"重新导入回不来"的，出路就该在同一页。
 * 默认折叠：这是不可逆的操作，不该跟搜索框、来源筛选这些日常控件摆在同一
 * 视觉层级上。
 *
 * ## 为什么列的是"存储里的类型"，不是上面分组摘要里的类型
 *
 * 分组摘要走合并视图，全被删掉的类型根本不出现——而那恰恰是最需要清空的
 * （demo 租户的 Order ID：存储 10000 行，可见 0 行）。所以这里单独读
 * /stored-types，并把"已删除但仍占着存储"这件事直接写出来。
 *
 * ## 三道闸
 *
 * 1. 先预览，说清会删多少、删什么、不删什么。
 * 2. 原样输入类型名，按钮才可点（服务端也校验一遍）。
 * 3. 提交预览时看到的数量；预览之后有人导入了新数据，服务端 409，这里重新
 *    拉一次预览让用户看新数字，不按新数字悄悄执行。
 */
export function TermTypePurgePanel({ sessionToken, tenantId, onPurged }: TermTypePurgePanelProps) {
  const [expanded, setExpanded] = useState(false)
  const [types, setTypes] = useState<StoredTermType[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [target, setTarget] = useState<string | null>(null)
  const [preview, setPreview] = useState<PurgePreview | null>(null)
  const [previewNotice, setPreviewNotice] = useState<string | null>(null)
  const [confirmText, setConfirmText] = useState('')
  const [purging, setPurging] = useState(false)
  const [purgeError, setPurgeError] = useState<string | null>(null)

  const loadTypes = async () => {
    setLoadError(null)
    try {
      setTypes(await fetchStoredTermTypes(sessionToken, tenantId))
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '加载失败')
    }
  }

  useEffect(() => {
    if (!expanded) return
    loadTypes().catch((err) => console.error(err))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded, tenantId, sessionToken])

  // 切租户时收起目标，避免拿着上一个租户的预览去清空这一个租户。
  useEffect(() => {
    setTarget(null)
    setPreview(null)
  }, [tenantId])

  const openPreview = async (termType: string, notice: string | null = null) => {
    setTarget(termType)
    setPreview(null)
    setPreviewNotice(notice)
    setConfirmText('')
    setPurgeError(null)
    try {
      setPreview(await previewPurgeTermType(sessionToken, tenantId, termType))
    } catch (err) {
      setPurgeError(err instanceof Error ? err.message : '预览失败')
    }
  }

  const handlePurge = async () => {
    if (!preview || confirmText !== preview.term_type) return
    setPurging(true)
    setPurgeError(null)
    try {
      const result = await purgeTermType(sessionToken, tenantId, {
        termType: preview.term_type,
        expectedNodeCount: preview.node_count,
        confirmText,
      })
      setTarget(null)
      setPreview(null)
      await loadTypes()
      onPurged(`已彻底清空「${preview.term_type}」：${result.node_count} 个实体`)
    } catch (err) {
      if (err instanceof PurgeCountChangedError) {
        await openPreview(preview.term_type, err.message)
      } else {
        setPurgeError(err instanceof Error ? err.message : '清空失败')
      }
    } finally {
      setPurging(false)
    }
  }

  return (
    <section
      data-testid="term-type-purge-panel"
      className="flex flex-col rounded-panel border border-status-error bg-card"
    >
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((prev) => !prev)}
        className={`flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-left ${focusRing}`}
      >
        <span className="font-bold text-ink">
          彻底清空某个实体类型
          <span className="ml-2 text-sm font-normal text-ink-soft">
            清理干净后从头重新导入时用。不可撤销。
          </span>
        </span>
        <span
          aria-hidden="true"
          className={`inline-block transition-transform duration-200 ${expanded ? 'rotate-0' : '-rotate-90'}`}
        >
          ▾
        </span>
      </button>

      {expanded && (
        <div className="flex flex-col gap-3 border-t border-subtle p-4 text-sm text-ink">
          <p className="text-ink-soft">
            在上面删除的实体只是被隐藏：词表行还在，重新导入也不会让它们回来。彻底清空会把这个类型在存储里的
            痕迹整个抹掉，之后的导入就相当于第一次导入。
          </p>

          {loadError && (
            <p role="alert" className="text-sm text-ink">
              {loadError}
            </p>
          )}
          {types === null && !loadError && <p className="text-ink-soft">正在读取存储里的实体类型…</p>}
          {types !== null && types.length === 0 && <p className="text-ink-soft">存储里没有任何实体。</p>}

          {types !== null && types.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[28rem] border-collapse text-left">
                <thead>
                  <tr className="border-b border-subtle text-xs text-ink-soft">
                    <th className="py-2 pr-3 font-bold">实体类型</th>
                    <th className="py-2 pr-3 text-right font-bold">存储行数</th>
                    <th className="py-2 pr-3 text-right font-bold">列表可见</th>
                    <th className="py-2" />
                  </tr>
                </thead>
                <tbody>
                  {types.map((t) => {
                    const hidden = t.stored - t.visible
                    return (
                      <tr key={t.term_type} className="border-b border-subtle last:border-b-0">
                        <td className="py-2 pr-3 font-mono">{t.term_type}</td>
                        <td className="py-2 pr-3 text-right tabular-nums">{t.stored}</td>
                        <td className="py-2 pr-3 text-right tabular-nums">
                          {t.visible}
                          {hidden > 0 && (
                            <span className="ml-2 text-xs text-ink-soft">（{hidden} 条已删除，重新导入不会恢复）</span>
                          )}
                        </td>
                        <td className="py-2 text-right">
                          <button
                            type="button"
                            data-testid={`purge-open-${t.term_type}`}
                            onClick={() => {
                              openPreview(t.term_type).catch((err) => console.error(err))
                            }}
                            className={`min-h-[36px] cursor-pointer rounded-control border border-status-error bg-paper px-3 text-sm font-bold text-ink ${focusRing}`}
                          >
                            清空…
                          </button>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}

          {target && (
            <div
              role="region"
              aria-label={`彻底清空「${target}」`}
              data-testid="purge-confirm"
              className="flex flex-col gap-3 rounded-card border border-status-error bg-paper p-3"
            >
              {previewNotice && (
                <p role="alert" className="text-sm text-ink">
                  {previewNotice}
                </p>
              )}
              {!preview && !purgeError && <p className="text-ink-soft">正在计算要删除多少…</p>}
              {preview && (
                <>
                  <p>
                    将彻底删除实体类型 <span className="font-mono font-bold">{preview.term_type}</span> 的{' '}
                    <span className="font-bold tabular-nums">{preview.node_count}</span> 个实体
                    {preview.created_only > 0 && (
                      <>（其中 {preview.created_only} 个是在后台手工新建的）</>
                    )}
                    ，连同它们在图谱里的节点和边、人工编辑记录、审核沉淀的别名、待处理的属性冲突和疑似重复建议。
                  </p>
                  <p className="text-ink-soft">
                    不删：实体类型本身的定义（本体结构不变）、稳定编号的分配记录（重新导入时同一个值拿到同一个编号）。
                  </p>
                  <label className="flex flex-col gap-1 font-bold">
                    <span>
                      输入 <span className="font-mono">{preview.term_type}</span> 确认
                    </span>
                    <input
                      type="text"
                      value={confirmText}
                      onChange={(e) => setConfirmText(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                      className={`w-full max-w-xs rounded-control border border-subtle bg-card px-3 py-2 font-mono font-normal text-ink focus:outline-none ${focusRing}`}
                    />
                  </label>
                </>
              )}
              {purgeError && (
                <p role="alert" className="text-sm text-ink">
                  {purgeError}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  data-testid="purge-execute"
                  onClick={() => {
                    handlePurge().catch((err) => console.error(err))
                  }}
                  disabled={!preview || confirmText !== preview.term_type || purging || preview.node_count === 0}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {purging ? '正在清空…' : '彻底清空，不可撤销'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setTarget(null)
                    setPreview(null)
                  }}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-card px-4 font-bold text-ink ${focusRing}`}
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
