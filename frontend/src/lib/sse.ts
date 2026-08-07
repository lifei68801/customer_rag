export interface ParsedSSEEvent {
  data: string
}

/**
 * 逐块读取 fetch Response 的 body 流，按 SSE 协议的空行分隔符（\n\n）
 * 切出每个事件，提取所有 `data:` 行拼接后返回。后端逐个事件只发一行
 * data（JSON.dumps 默认转义掉了字符串内的换行符），这里按行拼接是为了
 * 兼容 SSE 协议本身允许多行 data 的情况，不是假设后端会这样发。
 */
export async function* parseSSEStream(
  response: Response,
): AsyncGenerator<ParsedSSEEvent> {
  if (!response.body) {
    throw new Error('响应没有可读的 body，无法解析 SSE 流')
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    let separatorIndex = buffer.indexOf('\n\n')
    while (separatorIndex !== -1) {
      const rawEvent = buffer.slice(0, separatorIndex)
      buffer = buffer.slice(separatorIndex + 2)

      const dataLines = rawEvent
        .split('\n')
        .filter((line) => line.startsWith('data:'))
        .map((line) => line.slice(5).trimStart())

      if (dataLines.length > 0) {
        yield { data: dataLines.join('\n') }
      }

      separatorIndex = buffer.indexOf('\n\n')
    }
  }
}
