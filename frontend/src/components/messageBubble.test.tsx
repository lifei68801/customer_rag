import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageBubble } from './MessageBubble'
import type { ChatMessage } from '../hooks/useAgentChat'

/**
 * 流式期间屏幕上的内容只增不减。
 *
 * 此前每次工具调用（tool_status）都会把气泡清空：前一轮说过的话被挪进
 * reasoningTrail，而那是个默认折叠的 <details>，只在**答案落定后**才渲染。
 * 观感是"冒一句 → 擦掉 → 正在查询 → 再冒一句 → 再擦掉"。内容反复消失
 * 会让人觉得比实际更慢，读到一半的句子还会被抽走。
 */

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'm1',
    role: 'assistant',
    text: '',
    usedSources: [],
    reasoningTrail: [],
    isStreaming: false,
    ...overrides,
  } as ChatMessage
}

describe('回答气泡', () => {
  it('流式期间，前几轮说过的话留在屏幕上', () => {
    render(
      <MessageBubble
        message={message({
          isStreaming: true,
          reasoningTrail: ['让我查一下这个产品。'],
          text: '正在整理结果',
          statusText: '正在查询相关信息...',
        })}
      />,
    )

    expect(screen.getByTestId('reasoning-live').textContent).toContain('让我查一下这个产品。')
    expect(screen.getByText('正在查询相关信息...')).toBeTruthy()
    expect(screen.getByText(/正在整理结果/)).toBeTruthy()
  })

  it('已经开始出字时，"还在写"的指示器仍然在', () => {
    // 此前指示器只在没有文字时出现，一旦开始出字就消失——用户分不出
    // "写完了"还是"卡在这儿了"。
    render(<MessageBubble message={message({ isStreaming: true, text: '答案是' })} />)

    expect(screen.getByText(/答案是/)).toBeTruthy()
    expect(screen.getByTestId('thinking-indicator')).toBeTruthy()
  })

  it('答案落定后，推理过程收进折叠区，正文不被历史轮次挤占', () => {
    render(
      <MessageBubble
        message={message({
          isStreaming: false,
          reasoningTrail: ['让我查一下这个产品。'],
          text: '共有 3353 个订单。',
        })}
      />,
    )

    expect(screen.queryByTestId('reasoning-live')).toBeNull()
    expect(screen.getByText(/查看推理过程（1步）/)).toBeTruthy()
    expect(screen.getByText(/共有 3353 个订单。/)).toBeTruthy()
    // 写完了就不该还挂着"还在写"的指示器。
    expect(screen.queryByTestId('thinking-indicator')).toBeNull()
  })

  it('用户自己的消息不显示推理过程和指示器', () => {
    render(
      <MessageBubble
        message={message({ role: 'user', text: '有多少订单？', isStreaming: true })}
      />,
    )

    expect(screen.queryByTestId('reasoning-live')).toBeNull()
    expect(screen.queryByTestId('thinking-indicator')).toBeNull()
  })
})
