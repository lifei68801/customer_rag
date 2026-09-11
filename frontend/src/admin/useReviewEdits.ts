import { useCallback, useState } from 'react'

/**
 * 「知识图谱审核」页里，每条待审记录的编辑态。
 *
 * ## 为什么要有它
 *
 * 这份状态此前横切成六个 `Record<reviewId, …>` 字典：drafts / rejectNotes /
 * fixErrors / fixNotes / justCreated / ambiguityPick。于是「第 17 条记录现在
 * 处于什么状态」没有任何一个地方能回答，而它们之间有一条真实的不变量：
 *
 * > **改了某一侧的端点名字，那一侧的消歧选择和「新建」标记都得作废。**
 *
 * 名字变了，之前挑的那个类型是给旧名字挑的；之前那个「新建」标记指的是旧
 * 名字刚建出来的实体。留着它们，审核员会拿着为另一个实体做的决定去批准
 * 这条边——而界面上一切正常。
 *
 * 这条不变量此前靠**三个 setState 连着写**来维持，subject 一处、object 一处，
 * 两处各抄一遍。漏掉其中一个 setState 不会有任何报错。
 *
 * 现在它写在 `setEndpoint` 一个函数里。
 *
 * ## 这个 hook 管什么
 *
 * 只管每条记录的编辑态。列表本身、分页、哪条正在提交，都还在页面里——
 * 那些是页面级的东西，不是"这一条记录的状态"。
 */

/** 一条待审记录上，审核员改过的所有东西。 */
export interface ReviewEdit {
  /** 两侧端点的标准名（可编辑，初值来自后端的 suggested_*）。 */
  subject: string
  object: string
  /** 驳回备注。 */
  rejectNote: string
  /** 就地修复的报错和下一步提示。 */
  fixError: string
  fixNote: string
  /** 同名多类型时，审核员挑的那个类型。改名后作废。 */
  ambiguityPick: { subject?: string; object?: string }
  /** 通过「新建实体」流程现场创建的类型。改名后作废。 */
  justCreated: { subject?: string; object?: string }
}

export type Endpoint = 'subject' | 'object'

export function emptyReviewEdit(): ReviewEdit {
  return {
    subject: '',
    object: '',
    rejectNote: '',
    fixError: '',
    fixNote: '',
    ambiguityPick: {},
    justCreated: {},
  }
}

export interface ReviewEdits {
  /** 取某条记录的编辑态。没有就给一份空的——调用方不必到处写 `?.` 和 `?? ''`。 */
  get: (reviewId: number) => ReviewEdit
  /** 用后端返回的建议名初始化整批。已有的编辑态被丢弃（列表刚刷新过）。 */
  reset: (entries: { reviewId: number; subject: string; object: string }[]) => void
  /**
   * 改某一侧的端点名。
   *
   * **同时作废那一侧的消歧选择和「新建」标记**——这就是这个 hook 存在的
   * 理由。名字变了，之前为旧名字挑的类型和建的实体都不再适用；留着它们，
   * 审核员会拿着为另一个实体做的决定去批准这条边，而界面上一切正常。
   */
  setEndpoint: (reviewId: number, endpoint: Endpoint, value: string) => void
  /** 挑一个类型消歧。 */
  pickType: (reviewId: number, endpoint: Endpoint, termType: string) => void
  /** 记下这一侧是刚通过「新建实体」流程建出来的，以及建成了什么类型。 */
  markJustCreated: (reviewId: number, endpoint: Endpoint, termType: string) => void
  /** 改若干字段，不触发上面那条作废规则。 */
  patch: (reviewId: number, changes: Partial<ReviewEdit>) => void
  /** 丢掉一条记录的编辑态（它已经被批准/驳回，不在列表里了）。 */
  drop: (reviewId: number) => void
}

export function useReviewEdits(): ReviewEdits {
  const [edits, setEdits] = useState<Record<number, ReviewEdit>>({})

  const get = useCallback(
    (reviewId: number): ReviewEdit => edits[reviewId] ?? emptyReviewEdit(),
    [edits],
  )

  const reset = useCallback(
    (entries: { reviewId: number; subject: string; object: string }[]) => {
      setEdits(
        Object.fromEntries(
          entries.map((e) => [
            e.reviewId,
            { ...emptyReviewEdit(), subject: e.subject, object: e.object },
          ]),
        ),
      )
    },
    [],
  )

  const patch = useCallback((reviewId: number, changes: Partial<ReviewEdit>) => {
    setEdits((prev) => ({
      ...prev,
      [reviewId]: { ...(prev[reviewId] ?? emptyReviewEdit()), ...changes },
    }))
  }, [])

  const setEndpoint = useCallback(
    (reviewId: number, endpoint: Endpoint, value: string) => {
      setEdits((prev) => {
        const current = prev[reviewId] ?? emptyReviewEdit()
        return {
          ...prev,
          [reviewId]: {
            ...current,
            [endpoint]: value,
            // 这两行是这个函数的全部意义。分开写在三个 setState 里时，
            // 漏掉任何一个都不会报错。
            ambiguityPick: { ...current.ambiguityPick, [endpoint]: undefined },
            justCreated: { ...current.justCreated, [endpoint]: undefined },
          },
        }
      })
    },
    [],
  )

  const pickType = useCallback(
    (reviewId: number, endpoint: Endpoint, termType: string) => {
      setEdits((prev) => {
        const current = prev[reviewId] ?? emptyReviewEdit()
        return {
          ...prev,
          [reviewId]: {
            ...current,
            ambiguityPick: { ...current.ambiguityPick, [endpoint]: termType },
          },
        }
      })
    },
    [],
  )

  const markJustCreated = useCallback(
    (reviewId: number, endpoint: Endpoint, termType: string) => {
      setEdits((prev) => {
        const current = prev[reviewId] ?? emptyReviewEdit()
        return {
          ...prev,
          [reviewId]: {
            ...current,
            justCreated: { ...current.justCreated, [endpoint]: termType },
          },
        }
      })
    },
    [],
  )

  const drop = useCallback((reviewId: number) => {
    setEdits((prev) => {
      const next = { ...prev }
      delete next[reviewId]
      return next
    })
  }, [])

  return { get, reset, setEndpoint, pickType, markJustCreated, patch, drop }
}
