import { useCallback, useState } from 'react'

/**
 * 本体结构页里「实体类型」和「关系类型」两个 tab 共用的编辑状态机。
 *
 * ## 为什么要有它
 *
 * 这两个 tab 的表单流程一模一样（新建 / 改一行 / 删一行 / 把一个类型迁移成
 * 另一个），此前各自用八个布尔和字符串把这件事表达一遍：editingValue /
 * creating / savingValue / deletingValue / migratingFrom / migrateTarget /
 * migrating，另一个 tab 换成 editingType / savingType / deletingType。
 *
 * 八个变量表达的其实是两件正交的事：**哪个表单开着**、**有没有请求在飞**。
 * 拆成八个之后，非法组合（同时 creating 和 editing）在类型上是可表达的，而
 * 「同一时刻只能有一个操作在飞」这条不变量散在十几个 disabled 表达式和六个
 * 函数开头的早退判断里——并且**它们并不一致**：
 *
 * - 「编辑」按钮 `disabled={editingValue !== null}`
 * - 「删除」按钮 `disabled={deletingValue !== null || editingValue !== null}`
 * - 「迁移」按钮 `disabled={migrating}`
 *
 * 也就是说删除请求在飞的时候，迁移按钮照样能点。收进这里之后，不变量只写
 * 在 `run` 一处。
 *
 * ## 这个 hook 管什么、不管什么
 *
 * 管**状态机**：哪个表单开着、有没有请求在飞。
 *
 * 不管**数据**：列表本身、编辑中的草稿、迁移目标框里的文字，都还留在各自的
 * tab 里——它们是各自实体的形状，抽进来只会让这个 hook 变成一个什么都知道的
 * 泛型容器。
 */

/** 哪个表单开着。四种，互斥——这正是拆成布尔之后表达不出来的那件事。 */
export type FormMode =
  | { kind: 'idle' }
  | { kind: 'creating' }
  | { kind: 'editing'; key: string }
  | { kind: 'migrating'; from: string }

/** 正在飞的那个请求。null 表示空闲。 */
export type PendingAction = { action: 'save' | 'delete' | 'migrate'; key: string } | null

export interface EditableList {
  form: FormMode
  pending: PendingAction
  /** 有请求在飞。所有会改数据的按钮都该看它。 */
  busy: boolean
  openCreate: () => void
  openEdit: (key: string) => void
  openMigrate: (from: string) => void
  close: () => void
  /**
   * 跑一个会改数据的操作。
   *
   * 这是那条不变量唯一的住处：已经有请求在飞时直接不跑。返回值说明这次调用
   * 有没有真的执行——调用方需要区分"跑完了"和"被拦下了"时用得上。
   *
   * 不在这里 catch：错误处理各 tab 不同（有的要认 409 里的挡路术语），
   * 吞掉异常会让那些分支没法写。这里只保证 finally 里一定复位——否则一次
   * 失败会把整个 tab 永久锁在 busy 上。
   */
  run: (
    action: 'save' | 'delete' | 'migrate',
    key: string,
    fn: () => Promise<void>,
  ) => Promise<boolean>
  isEditing: (key: string) => boolean
  isSaving: (key: string) => boolean
  isDeleting: (key: string) => boolean
}

export function useEditableList(): EditableList {
  const [form, setForm] = useState<FormMode>({ kind: 'idle' })
  const [pending, setPending] = useState<PendingAction>(null)

  const busy = pending !== null

  const openCreate = useCallback(() => setForm({ kind: 'creating' }), [])
  const openEdit = useCallback((key: string) => setForm({ kind: 'editing', key }), [])
  const openMigrate = useCallback((from: string) => setForm({ kind: 'migrating', from }), [])
  const close = useCallback(() => setForm({ kind: 'idle' }), [])

  const run = useCallback(
    async (
      action: 'save' | 'delete' | 'migrate',
      key: string,
      fn: () => Promise<void>,
    ): Promise<boolean> => {
      // 按钮的 disabled 只是提示，不是防线：键盘、表单回车、以及一次点击
      // 在 React 把 disabled 渲染上去之前的那一瞬，都能绕过它。
      if (pending !== null) return false
      setPending({ action, key })
      try {
        await fn()
        return true
      } finally {
        setPending(null)
      }
    },
    [pending],
  )

  const isEditing = useCallback(
    (key: string) => form.kind === 'editing' && form.key === key,
    [form],
  )
  const isSaving = useCallback(
    (key: string) => pending?.action === 'save' && pending.key === key,
    [pending],
  )
  const isDeleting = useCallback(
    (key: string) => pending?.action === 'delete' && pending.key === key,
    [pending],
  )

  return {
    form,
    pending,
    busy,
    openCreate,
    openEdit,
    openMigrate,
    close,
    run,
    isEditing,
    isSaving,
    isDeleting,
  }
}
