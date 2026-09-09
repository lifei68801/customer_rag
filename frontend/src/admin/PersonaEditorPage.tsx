import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { PAGE_TITLES } from '../adminRoutes'
import { adminFetch, extractErrorDetail } from './adminApi'
import { Skeleton } from './Skeleton'
import { useAdminAuth } from './useAdminAuth'
import { useAdminTenant } from './TenantContext'
import { useToast } from './ToastContext'
// 回包形状只写一份：手抄两份的话，后端加字段时只有一份会跟上，
// 而两份都是 `as` 断言出来的，编译器不会说话。
import type { PersonaDetail, PersonaSource } from '../lib/personasApi'

/** 引导问题的条数上限。超过六条前台一屏放不下，读者也不会挨条看完。 */
const MAX_QUESTIONS = 6

const card = 'rounded-card border border-subtle bg-card p-4'
const focusRing =
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'
const inputClass = `rounded-control border border-subtle bg-paper px-3 py-2 text-sm text-ink placeholder:text-ink-soft ${focusRing}`
const buttonClass = `min-h-[36px] cursor-pointer rounded-control border border-subtle bg-paper px-3 text-sm font-bold text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`
const iconButtonClass = `flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-control border border-subtle bg-paper text-ink transition hover:bg-interactive-hover disabled:cursor-not-allowed disabled:opacity-40 ${focusRing}`

/**
 * 一条引导问题。带一个只活在前端的 `id`。
 *
 * 用数组下标当 React key 会在增删和调序时把输入框的身份跟位置绑死：
 * 上移之后焦点留在原来那个位置上，而那个位置现在是被挤下来的另一条
 * ——键盘用户连按两下 Enter，第二下动的是别人，两下之后原地不动
 * （personaEditor.test.tsx 有这条回归用例）。
 */
interface QuestionRow {
  id: string
  text: string
}

let rowSeq = 0
const newRow = (text: string): QuestionRow => ({ id: `q${(rowSeq += 1)}`, text })

/**
 * 数字人编辑页：这个知识库对外是谁、可以问它什么。
 *
 * 引导问题的成立与否由后端判定（保存时跑一遍实体匹配），这一页只负责
 * **把后端说的话原样转达**。它自己不做校验——判断依据是本体和图，都在
 * 后端手里，前端复制一份只会得到一份会过期的判断。
 *
 * 两处最容易做错的地方：
 *
 * 一是保存被拒时。后端的 400 里点名了是哪几条不成立，那句话里有用户需要
 * 的全部信息；包装成「保存失败」等于把它扔掉。而且被拒之后表单内容必须
 * 原样留着——清空的话，配了六条被拒一条的人要全部重打。
 *
 * 二是加载失败时。给一张空表单会被读成「还没配过」，照着它保存一次，真
 * 配过的内容就被空值覆盖了。所以加载失败时不渲染表单，只说发生了什么。
 */
export function PersonaEditorPage() {
  const { sessionToken } = useAdminAuth()
  const { tenantId } = useAdminTenant()
  const showToast = useToast()

  const [name, setName] = useState('')
  const [avatar, setAvatar] = useState('')
  const [tagline, setTagline] = useState('')
  const [questions, setQuestions] = useState<QuestionRow[]>([])
  const [source, setSource] = useState<PersonaSource>('handwritten')
  const [stale, setStale] = useState<string[]>([])
  const [staleError, setStaleError] = useState<string | null>(null)
  const [detailLoaded, setDetailLoaded] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    document.title = `${PAGE_TITLES.persona} · 管理后台`
  }, [])

  const loadDetail = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    try {
      const response = await adminFetch(`/api/admin/${encodeURIComponent(tenantId)}/persona`, sessionToken)
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '数字人信息加载失败'))
      }
      const detail = (await response.json()) as PersonaDetail
      setName(detail.name)
      setAvatar(detail.avatar)
      setTagline(detail.tagline)
      setQuestions(detail.questions.map(newRow))
      setSource(detail.questions_source)
      setLoadError(null)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : '数字人信息加载失败')
    } finally {
      setDetailLoaded(true)
    }
  }, [sessionToken, tenantId])

  const loadStale = useCallback(async () => {
    if (!sessionToken || !tenantId) return
    setStaleError(null)
    try {
      const response = await adminFetch(
        `/api/admin/${encodeURIComponent(tenantId)}/persona/stale-questions`,
        sessionToken,
      )
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(extractErrorDetail(body, '失效检测没跑成功'))
      }
      const body = (await response.json()) as { stale: string[] }
      setStale(body.stale)
      setStaleError(null)
    } catch (err) {
      // 不能留成空数组了事——那读起来就是「一条都没失效」，而真相是根本
      // 没查成。就地摆一条常驻提示加一个重试入口：这一页有的是地方放它，
      // 用会自动消失的 toast 等于赌用户那几秒正好在看屏幕。
      setStale([])
      setStaleError(err instanceof Error ? err.message : '失效检测没跑成功')
    }
  }, [sessionToken, tenantId])

  // 两个请求互不依赖，并发发出去；首屏耗时是两者里慢的那个，不是两者之和。
  // 失败的处置也不同：详情拉不到就不给表单，失效清单拉不到只是少一条提示。
  useEffect(() => {
    void Promise.all([loadDetail(), loadStale()])
  }, [loadDetail, loadStale])

  const updateQuestion = (index: number, value: string) => {
    setQuestions((prev) => prev.map((q, i) => (i === index ? { ...q, text: value } : q)))
  }

  const removeQuestion = (index: number) => {
    setQuestions((prev) => prev.filter((_, i) => i !== index))
  }

  // 焦点不用手工搬：每一行的 key 是这条问题自己的 id，React 因此把 DOM
  // 节点跟着它一起挪，焦点自然留在被移动的那一条上。
  //
  // 未覆盖的一处：移到头尾时同方向的按钮会禁用，真实浏览器会让焦点脱落，
  // 而 jsdom 不复现这个行为——这条只能人工在浏览器里看。
  const moveQuestion = (index: number, delta: number) => {
    setQuestions((prev) => {
      const target = index + delta
      if (target < 0 || target >= prev.length) return prev
      const next = [...prev]
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
  }

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    if (!sessionToken || !tenantId) return
    setSaving(true)
    try {
      const response = await adminFetch(`/api/admin/${encodeURIComponent(tenantId)}/persona`, sessionToken, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        // 空行不发出去：用户加了一条又没填，那不是「配了一条空问题」。
        body: JSON.stringify({
          avatar,
          tagline,
          questions: questions.map((q) => q.text.trim()).filter(Boolean),
        }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        // 原样转达后端点名的那几条。这里绝不能换成一句「保存失败」。
        throw new Error(extractErrorDetail(body, '保存失败'))
      }
      // 存成功之后这批就是手写的了，哪怕内容是从自动那批原样留下来的。
      setSource('handwritten')
      setSaveError(null)
      showToast('已保存')
      // 只重算失效标记，不回头重拉详情。刚存进去的内容就在手上，再拉一次
      // 只是多一个失败面——那次 GET 失败会把刚存好的表单整页替换成「加载
      // 失败」，用户读到的是「保存失败」，而其实存成功了。
      void loadStale()
    } catch (err) {
      // 表单内容一个字都不动。清空的话，配了六条被拒一条的人要全部重打。
      setSaveError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const header = (
    <div className="flex flex-col gap-1">
      <h1 className="font-mono text-xl font-semibold text-ink">{PAGE_TITLES.persona}</h1>
      <p className="text-sm text-ink-soft">
        前台右栏和空会话首屏显示的就是这里配的内容。引导问题点了必须答得出来，
        所以保存时后端会拿当前本体核一遍。
      </p>
    </div>
  )

  if (!detailLoaded) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        <Skeleton variant="card-list" count={2} />
      </div>
    )
  }

  if (loadError !== null) {
    return (
      <div className="flex flex-col gap-6">
        {header}
        {/* 不渲染表单：一张空表单会被读成「还没配过」，照着它保存一次就把
            真配过的内容覆盖成空值了。 */}
        <div role="status" className={`${card} flex flex-col items-start gap-3`}>
          <p className="text-sm text-status-error-strong">{loadError}</p>
          <button type="button" className={buttonClass} onClick={() => void loadDetail()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  const atLimit = questions.length >= MAX_QUESTIONS

  return (
    <form className="flex max-w-2xl flex-col gap-6" onSubmit={handleSave}>
      {header}

      <div className={`${card} flex flex-col gap-4`}>
        <div className="flex flex-col gap-1">
          <span className="text-sm font-bold text-ink">名字</span>
          {/* 名字取自租户名，不在这里改：它是租户的身份，改它会让「我在哪个
              知识库」这件事在后台和前台各说各的。 */}
          <p className="text-sm text-ink-soft">{name}——跟随租户名，要改请去租户管理。</p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-bold text-ink">头像</span>
          <input
            className={`${inputClass} w-20 text-center text-lg`}
            value={avatar}
            maxLength={2}
            onChange={(e) => setAvatar(e.target.value)}
            placeholder="🛍️"
          />
          <span className="text-xs text-ink-soft">一到两个 emoji。不填时前台显示一个占位符。</span>
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-sm font-bold text-ink">人设</span>
          <input
            aria-label="人设"
            className={inputClass}
            value={tagline}
            onChange={(e) => setTagline(e.target.value)}
            placeholder="我知道商品、口味和产地"
          />
          <span className="text-xs text-ink-soft">一句话说清这个知识库里有什么。</span>
        </label>
      </div>

      <div className={`${card} flex flex-col gap-3`}>
        <div className="flex flex-col gap-1">
          <span className="text-sm font-bold text-ink">引导问题</span>
          <span className="text-xs text-ink-soft">
            前台空会话时列出来，点一下就直接问出去。顺序有意义——第一条最显眼。
          </span>
          {source === 'generated' && (
            <p role="status" className="text-xs text-ink-soft">
              下面这些是根据本体自动生成的，还没人手写过。一旦保存就固定下来，
              之后不再随本体变化。
            </p>
          )}
          {source === 'unavailable' && (
            // 这一条对终端用户不存在——前台该看到的就是一个诚实的空引导区。
            // 但管理员必须看得出这是故障而不是「本体里还没东西可问」，
            // 他是唯一修得了图谱连接的人。
            <p role="status" className="flex items-center gap-1 text-xs text-status-error-strong">
              <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
              自动生成没跑成功：图谱查不通。下面的空不代表本体里没东西——
              这是一个要修的故障，前台此刻不显示任何引导问题。
            </p>
          )}
        </div>

        {staleError !== null && (
          <div role="status" className="flex flex-wrap items-center gap-2 text-xs text-status-error-strong">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{staleError}——下面这几条里可能有已经答不出来的。</span>
            <button type="button" className={buttonClass} onClick={() => void loadStale()}>
              重新检测
            </button>
          </div>
        )}

        {/* 图谱不通那一档不说这句：上面那条红字已经说清楚了，再补一句
            「这个数字人不显示任何引导问题」会读成"这是配置的结果"，
            而它其实是一个故障。 */}
        {questions.length === 0 && source !== 'unavailable' && (
          // 这句话要分两种情况说，说错就是骗人：还没保存过的时候，前台
          // 显示的仍是自动生成的那批；保存了空列表之后，前台才真的空着。
          <p className="text-sm text-ink-soft">
            {source === 'generated'
              ? '一条引导问题都没有。就这样保存，等于说「这个数字人不要引导问题」，前台会空着；不保存的话，前台继续显示上面那批自动生成的。'
              : '这个数字人不显示任何引导问题。'}
          </p>
        )}

        {questions.map((row, index) => {
          const isStale = stale.includes(row.text)
          return (
            <div key={row.id} data-testid={`question-row-${index}`} className="flex flex-col gap-1">
              <div className="flex items-center gap-2">
                <input
                  aria-label={`引导问题第 ${index + 1} 条`}
                  className={`${inputClass} flex-1`}
                  value={row.text}
                  onChange={(e) => updateQuestion(index, e.target.value)}
                />
                <button
                  type="button"
                  aria-label={`上移第 ${index + 1} 条`}
                  className={iconButtonClass}
                  disabled={index === 0}
                  onClick={() => moveQuestion(index, -1)}
                >
                  <ArrowUp className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  aria-label={`下移第 ${index + 1} 条`}
                  className={iconButtonClass}
                  disabled={index === questions.length - 1}
                  onClick={() => moveQuestion(index, 1)}
                >
                  <ArrowDown className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  type="button"
                  aria-label={`删除第 ${index + 1} 条`}
                  className={iconButtonClass}
                  onClick={() => removeQuestion(index)}
                >
                  <Trash2 className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
              {isStale && (
                // 标记挂在这一行上，不是页面顶上飘一句：六条里哪条坏了，
                // 用户不该靠猜。
                <p className="flex items-center gap-1 text-xs text-status-error-strong">
                  <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" />
                  已失效——本体改动之后这条不再命中，点了答不出来。
                </p>
              )}
            </div>
          )
        })}

        <div className="flex items-center gap-3">
          <button
            type="button"
            className={buttonClass}
            disabled={atLimit}
            onClick={() => setQuestions((prev) => [...prev, newRow('')])}
            aria-label="添加一条引导问题"
          >
            <span className="flex items-center gap-1.5">
              <Plus className="h-4 w-4" aria-hidden="true" />
              添加一条
            </span>
          </button>
          {/* 禁用了还要说清为什么。点不动且不说原因，用户会以为界面坏了。 */}
          {atLimit && (
            <span className="text-xs text-ink-soft">已达上限 6 条，删掉一条才能再加。</span>
          )}
        </div>
      </div>

      {saveError !== null && (
        <p role="status" className={`${card} text-sm text-status-error-strong`}>
          {saveError}
        </p>
      )}

      <div>
        <button type="submit" className={buttonClass} disabled={saving}>
          保存
        </button>
      </div>
    </form>
  )
}
