import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  WorkspaceConflictError,
  createWorkspace,
  deleteWorkspace,
  exportSkill,
  fetchGrounding,
  fetchSkills,
  fetchWorkspace,
  previewApply,
  saveWorkspace,
} from './workspaceApi'
import type { WorkspaceState } from './types'

const EMPTY_STATE: WorkspaceState = {
  term_types: [],
  relation_types: [],
  constraints: [],
  sources: [],
  unmatched_columns: {},
  questions: [],
}

let calls: { url: string; init: RequestInit | undefined }[] = []

function stubFetch(responder: (url: string) => Response) {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init })
      return Promise.resolve(responder(String(input)))
    }),
  )
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('workspaceApi', () => {
  it('租户 id 进路径时做过转义', async () => {
    stubFetch(() => json({ skills: [] }))
    await fetchSkills('a/b', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/a%2Fb/modeling-workspace/skills')
  })

  it('没有工作区时返回 null，不抛', async () => {
    stubFetch(() => json({ workspace: null }))
    expect(await fetchWorkspace('t1', 'tok')).toBeNull()
  })

  it('建工作区把 skill_name 放进 body', async () => {
    stubFetch(() =>
      json({
        workspace: {
          tenant_id: 't1',
          skill_name: 'consumer_retail',
          skill_version: '1',
          state: EMPTY_STATE,
          updated_at: 'now',
          updated_by: 'alice',
        },
      }),
    )
    const workspace = await createWorkspace('t1', 'tok', 'consumer_retail')
    expect(workspace.skill_name).toBe('consumer_retail')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ skill_name: 'consumer_retail' })
  })

  it('保存带上 updated_at 做乐观锁', async () => {
    stubFetch(() =>
      json({
        workspace: {
          tenant_id: 't1',
          skill_name: null,
          skill_version: null,
          state: EMPTY_STATE,
          updated_at: 'later',
          updated_by: 'alice',
        },
      }),
    )
    await saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')
    expect(JSON.parse(String(calls[0].init?.body)).updated_at).toBe('earlier')
  })

  it('保存撞上 409 时抛 WorkspaceConflictError，带服务端那句话', async () => {
    // 普通 Error 的话，页面没法把"别人改过，请刷新"跟"网络挂了"分开——
    // 前者要提示刷新，后者要提示重试
    stubFetch(() => json({ detail: '工作区在 later 被 bob 改过' }, 409))
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toBeInstanceOf(
      WorkspaceConflictError,
    )
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toThrow('bob')
  })

  it('其它错误码抛普通 Error，带 detail', async () => {
    stubFetch(() => json({ detail: 'provenance llm 不合法' }, 400))
    await expect(saveWorkspace('t1', 'tok', EMPTY_STATE, 'x')).rejects.toThrow('llm')
  })

  it('apply-preview 把三段原样发出去', async () => {
    stubFetch(() =>
      json({
        added_term_types: ['SKU'],
        removed_term_types: [],
        changed_term_types: [],
        added_relation_types: [],
        removed_relation_types: [],
        added_constraints: [],
        removed_constraints: [],
      }),
    )
    const diff = await previewApply('t1', 'tok', {
      term_types: [{ value: 'SKU', extra_fields: [], standard_name_value_type: 'string' }],
      relation_types: [],
      constraints: [],
    })
    expect(diff.added_term_types).toEqual(['SKU'])
    expect(JSON.parse(String(calls[0].init?.body)).term_types[0].value).toBe('SKU')
  })

  it('删除工作区发 DELETE 到工作区路径', async () => {
    stubFetch(() => json({}))
    await deleteWorkspace('t1', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/modeling-workspace')
    expect(calls[0].init?.method).toBe('DELETE')
  })

  it('读落地状态走 /grounding，原样返回扁平对象', async () => {
    const body = {
      status: 'confirmed',
      grounded_term_types: ['SKU'],
      grounded_relation_types: [],
      source_files: ['sku.csv'],
      parse_error: null,
    }
    stubFetch(() => json(body))
    const grounding = await fetchGrounding('t1', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/modeling-workspace/grounding')
    expect(grounding).toEqual(body)
  })

  it('导出返回 YAML 文本', async () => {
    stubFetch(() => json({ yaml: 'name: t1_domain\n' }))
    expect(await exportSkill('t1', 'tok', 't1_domain', 'T1 领域')).toContain('name: t1_domain')
  })
})
