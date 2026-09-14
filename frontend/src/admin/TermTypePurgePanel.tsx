import { useEffect, useState } from 'react'
import { useConfirm } from './ConfirmContext'
import {
  fetchStoredTermTypes,
  previewPurgeTermType,
  purgeTermType,
  PurgeCountChangedError,
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

/** 一个类型清空完的结果。失败的那些要逐条报出来，不能只说"部分失败"。 */
interface PurgeOutcome {
  termType: string
  nodeCount: number
  error: string | null
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
 * ## 闸只留一道，放在批量那一层
 *
 * 第一版是每个类型各走一遍：预览 → 手打类型名 → 执行。demo 有 12 个类型，
 * 那就是 12 遍——而用户的处境恰恰是"整个库要清干净重导"，逐个确认没有让他
 * 更安全，只是让他更容易在第 8 遍时不再读弹窗。
 *
 * 现在是多选（含全选）→ 一个按钮 → 一次确认弹窗，弹窗里写清一共几个类型、
 * 多少个实体、删掉的是什么、什么不删。服务端那道"手打类型名"的校验保留
 * 不动（它挡的是绕过界面直接调接口的人），由这里按类型逐个填。
 *
 * **仍然逐个类型调接口**，不合并成一个大事务：每个类型各自带着"预览时看到
 * 多少个"的数量守卫，一个类型在这期间被别人导入了新数据，只有它会被拒绝，
 * 其余照常清完；合并之后只能整批拒绝，而用户不知道是哪一个变了。
 */
export function TermTypePurgePanel({ sessionToken, tenantId, onPurged }: TermTypePurgePanelProps) {
  const confirm = useConfirm()
  const [expanded, setExpanded] = useState(false)
  const [types, setTypes] = useState<StoredTermType[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [purgingOf, setPurgingOf] = useState<string | null>(null)
  const [outcomes, setOutcomes] = useState<PurgeOutcome[] | null>(null)

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

  // 切租户时清掉选择：拿着上一个租户选中的类型名去清这一个租户，名字还可能
  // 正好同名。
  useEffect(() => {
    setSelected(new Set())
    setOutcomes(null)
  }, [tenantId])

  const toggle = (termType: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(termType)) next.delete(termType)
      else next.add(termType)
      return next
    })
  }

  const rows = types ?? []
  const chosen = rows.filter((t) => selected.has(t.term_type))
  const chosenEntities = chosen.reduce((sum, t) => sum + t.stored, 0)
  const allSelected = rows.length > 0 && chosen.length === rows.length

  const handlePurgeSelected = async () => {
    if (chosen.length === 0) return
    const names = chosen.map((t) => t.term_type).join('、')
    const ok = await confirm({
      message:
        `彻底清空 ${chosen.length} 个实体类型（${names}），共 ${chosenEntities.toLocaleString()} 个实体。\n\n` +
        '连同它们在图谱里的节点和边、人工编辑记录、审核沉淀的别名、待处理的属性冲突和疑似重复建议一起删除。\n' +
        '不删：实体类型本身的定义（本体结构不变）、稳定编号的分配记录。\n\n' +
        '删除后无法撤销。之后重新导入会从零重建。',
      confirmLabel: '彻底清空',
    })
    if (!ok) return

    const results: PurgeOutcome[] = []
    for (const type of chosen) {
      setPurgingOf(type.term_type)
      try {
        // 每个类型现取一次预览：用列表里那个数当守卫的话，它可能是几分钟前
        // 拉的，而守卫的意义正是"跟我刚才看到的一致"。这一次取到的是最新值，
        // 再变就会被服务端拒绝。
        const preview = await previewPurgeTermType(sessionToken, tenantId, type.term_type)
        const result = await purgeTermType(sessionToken, tenantId, {
          termType: type.term_type,
          expectedNodeCount: preview.node_count,
          confirmText: type.term_type,
        })
        results.push({ termType: type.term_type, nodeCount: result.node_count, error: null })
      } catch (err) {
        const message =
          err instanceof PurgeCountChangedError
            ? `${err.message}（这个类型没有清空，其余照常）`
            : err instanceof Error
              ? err.message
              : '清空失败'
        results.push({ termType: type.term_type, nodeCount: 0, error: message })
      }
    }
    setPurgingOf(null)
    setOutcomes(results)
    setSelected(new Set())
    await loadTypes()

    const cleared = results.filter((r) => r.error === null)
    const total = cleared.reduce((sum, r) => sum + r.nodeCount, 0)
    onPurged(
      cleared.length === results.length
        ? `已彻底清空 ${cleared.length} 个类型，共 ${total.toLocaleString()} 个实体`
        : `清空了 ${cleared.length}/${results.length} 个类型，有失败的，见下面的明细`,
    )
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
          彻底清空实体类型
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
            在上面删除的实体只是被隐藏：词表行还在，重新导入也不会让它们回来。彻底清空会把这些类型在存储里的
            痕迹整个抹掉，之后的导入就相当于第一次导入。
          </p>

          {loadError && (
            <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-ink">
              <span>{loadError}</span>
              <button
                type="button"
                onClick={() => void loadTypes()}
                className={`font-bold underline ${focusRing}`}
              >
                重试
              </button>
            </p>
          )}
          {types === null && !loadError && <p className="text-ink-soft">正在读取存储里的实体类型…</p>}
          {types !== null && rows.length === 0 && <p className="text-ink-soft">存储里没有任何实体。</p>}

          {rows.length > 0 && (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[28rem] border-collapse text-left">
                  <thead>
                    <tr className="border-b border-subtle text-xs text-ink-soft">
                      <th className="py-2 pr-3 font-bold">
                        <label className="flex cursor-pointer items-center gap-2">
                          <input
                            type="checkbox"
                            data-testid="purge-select-all"
                            checked={allSelected}
                            onChange={() =>
                              setSelected(allSelected ? new Set() : new Set(rows.map((t) => t.term_type)))
                            }
                          />
                          全选
                        </label>
                      </th>
                      <th className="py-2 pr-3 font-bold">实体类型</th>
                      <th className="py-2 pr-3 text-right font-bold">存储行数</th>
                      <th className="py-2 text-right font-bold">列表可见</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((t) => {
                      const hidden = t.stored - t.visible
                      return (
                        <tr key={t.term_type} className="border-b border-subtle last:border-b-0">
                          <td className="py-2 pr-3">
                            <input
                              type="checkbox"
                              aria-label={`选择 ${t.term_type}`}
                              data-testid={`purge-select-${t.term_type}`}
                              checked={selected.has(t.term_type)}
                              onChange={() => toggle(t.term_type)}
                            />
                          </td>
                          <td className="py-2 pr-3 font-mono">{t.term_type}</td>
                          <td className="py-2 pr-3 text-right tabular-nums">
                            {t.stored.toLocaleString()}
                          </td>
                          <td className="py-2 text-right tabular-nums">
                            {t.visible.toLocaleString()}
                            {hidden > 0 && (
                              <span className="ml-2 text-xs text-ink-soft">
                                （{hidden.toLocaleString()} 条已删除，重新导入不会恢复）
                              </span>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  data-testid="purge-selected"
                  onClick={() => {
                    handlePurgeSelected().catch((err) => console.error(err))
                  }}
                  disabled={chosen.length === 0 || purgingOf !== null}
                  className={`min-h-[44px] cursor-pointer rounded-control border border-subtle bg-status-error-strong px-4 font-bold text-white transition active:scale-95 active:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`}
                >
                  {purgingOf !== null
                    ? `正在清空「${purgingOf}」…`
                    : `彻底清空所选（${chosen.length} 个类型 · ${chosenEntities.toLocaleString()} 个实体）`}
                </button>
                {chosen.length === 0 && <span className="text-xs text-ink-soft">先勾选要清空的类型</span>}
              </div>
            </>
          )}

          {outcomes && (
            <ul data-testid="purge-outcomes" className="flex flex-col gap-1 text-xs">
              {outcomes.map((outcome) => (
                <li
                  key={outcome.termType}
                  className={outcome.error === null ? 'text-ink-soft' : 'text-status-error-strong'}
                >
                  <span className="font-mono">{outcome.termType}</span>：
                  {outcome.error === null
                    ? `已清空 ${outcome.nodeCount.toLocaleString()} 个实体`
                    : outcome.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}
