import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  InterviewConflictError,
  addQuestion,
  answerInterview,
  deleteInterview,
  fetchInterview,
  previewDraft,
  saveInterview,
  startInterview,
} from './interviewApi'
import type { InterviewSession, InterviewState } from './types'

const EMPTY_STATE: InterviewState = {
  turns: [],
  skeleton: { term_types: [], relation_types: [], constraints: [] },
  questions: [],
  done: false,
}

function session(): InterviewSession {
  return {
    tenant_id: 't1',
    state: EMPTY_STATE,
    updated_at: 'now',
    updated_by: 'alice',
  }
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

describe('interviewApi', () => {
  it('租户 id 进路径时做过转义', async () => {
    stubFetch(() => json({ session: null }))
    await fetchInterview('a/b', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/a%2Fb/interview')
  })

  it('没有会话时返回 null，不抛', async () => {
    stubFetch(() => json({ session: null }))
    expect(await fetchInterview('t1', 'tok')).toBeNull()
  })

  it('开始访谈发 POST 到 /interview', async () => {
    stubFetch(() => json({ session: session() }))
    const result = await startInterview('t1', 'tok')
    expect(result.tenant_id).toBe('t1')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/interview')
    expect(calls[0].init?.method).toBe('POST')
  })

  it('提交回答走 /interview/answer，body 带 answer 和 updated_at', async () => {
    stubFetch(() => json({ session: session(), turn: { question: '下一个问题', added_count: 1, dropped: 0, note: null } }))
    const result = await answerInterview('t1', 'tok', '我们卖服装', 'earlier')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/interview/answer')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ answer: '我们卖服装', updated_at: 'earlier' })
    expect(result.turn.added_count).toBe(1)
  })

  it('保存带上 updated_at 做乐观锁', async () => {
    stubFetch(() => json({ session: session() }))
    await saveInterview('t1', 'tok', EMPTY_STATE, 'earlier')
    expect(calls[0].init?.method).toBe('PUT')
    expect(JSON.parse(String(calls[0].init?.body)).updated_at).toBe('earlier')
  })

  it('保存撞上 409 时抛 InterviewConflictError，带服务端那句话', async () => {
    stubFetch(() => json({ detail: '访谈在 later 被 bob 改过' }, 409))
    await expect(saveInterview('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toBeInstanceOf(
      InterviewConflictError,
    )
    await expect(saveInterview('t1', 'tok', EMPTY_STATE, 'earlier')).rejects.toThrow('bob')
  })

  it('answerInterview 撞上 409 同样抛 InterviewConflictError', async () => {
    stubFetch(() => json({ detail: '访谈已被改过' }, 409))
    await expect(answerInterview('t1', 'tok', '答案', 'earlier')).rejects.toBeInstanceOf(
      InterviewConflictError,
    )
  })

  it('其它错误码抛普通 Error，带 detail', async () => {
    stubFetch(() => json({ detail: '回答不能为空' }, 400))
    await expect(answerInterview('t1', 'tok', '', 'x')).rejects.toThrow('回答不能为空')
  })

  it('删除访谈发 DELETE 到 /interview', async () => {
    stubFetch(() => json({ deleted: true }))
    await deleteInterview('t1', 'tok')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/interview')
    expect(calls[0].init?.method).toBe('DELETE')
  })

  it('addQuestion 走 /interview/questions，body 带 text 和 updated_at', async () => {
    stubFetch(() =>
      json({
        session: session(),
        question: { text: '哪个品类卖得最好', needs: { term_types: ['品类'], relation_types: [] }, missing: ['品类'], at: 'now' },
      }),
    )
    const result = await addQuestion('t1', 'tok', '哪个品类卖得最好', 'earlier')
    expect(calls[0].url).toContain('/api/admin/ontology/t1/interview/questions')
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ text: '哪个品类卖得最好', updated_at: 'earlier' })
    expect(result.question.missing).toEqual(['品类'])
  })

  it('previewDraft 复用 apply-preview 端点，把 payload 原样发出去', async () => {
    stubFetch(() =>
      json({
        added_term_types: ['商品'],
        removed_term_types: [],
        changed_term_types: [],
        added_relation_types: [],
        removed_relation_types: [],
        added_constraints: [],
        removed_constraints: [],
      }),
    )
    const diff = await previewDraft('t1', 'tok', {
      term_types: [{ value: '商品', extra_fields: [], standard_name_value_type: 'string' }],
      relation_types: [],
      constraints: [],
    })
    expect(calls[0].url).toContain('/api/admin/ontology/t1/modeling-workspace/apply-preview')
    expect(diff.added_term_types).toEqual(['商品'])
    expect(JSON.parse(String(calls[0].init?.body)).term_types[0].value).toBe('商品')
  })
})
