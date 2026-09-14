"""给**文字**流式输出用的切分与轻量安全闸。

## 为什么不复用 stream_sentences

`app/voice/streaming_responder.py` 的 `stream_sentences` 把 token 增量攒到
句末标点（只认 `。！？!?`）才推一次。语音路径必须这样——TTS 按句合成，切碎
了音频会一顿一顿。但文字路径套用同一套切分有两个后果：

- **长句和列表会攒住**。一句 40 字的长从句里一个句末标点都没有，用户盯着
  空白直到整句落地；markdown 列表（`- 订单 0-556-54422-6：¥1,200`）更极端，
  整张表可能一个句末标点都没有，全部攒到流结束才 flush。
- **格式被 strip 掉**。split_sentences 会 strip 每一段，换行和缩进在流式
  阶段丢失，markdown 列表在推送过程中是糊成一行的（最终 final 事件带的是
  完整原文，所以只有流式那段时间里是坏的）。

所以文字路径单独一套：按更密的边界切，且**逐字保留原文**。

## 安全闸为什么要带重叠窗口

轻量规则检查（手机号/身份证正则）此前按整句跑。切碎之后，一个完整手机号
可能被切成两块，两块各自都匹配不上正则，于是漏出去——这不是理论风险，是
切小这件事直接引入的。

所以检查的不是"这一小块"，而是"**已经放行的末尾 N 个字 + 这一小块**"：
被切开的号码在第二块到达时仍然会被抓住。代价是前半截已经显示出去了，但
半个号码不构成完整 PII；而且整段回复生成完之后还有一次完整的语义级审查
（output_safety_node），它会把整条回答替换掉。

N 取 24：比最长的身份证号（18 位）还长，够覆盖任何一种被切开的号码。
"""

from __future__ import annotations

from typing import AsyncIterator

from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE, check_text

#: 这些标点一出现就把缓冲区推出去。比 stream_sentences 的句末标点多了次级
#: 标点和换行——它们是中文长句里真正的停顿点，也是 markdown 列表的行边界。
DISPLAY_FLUSH_CHARS = "。！？!?，、；：,;:\n"

#: 一个标点都没有时，攒到这么多字也推出去。取 16：短到让人感觉在"流"，
#: 又不至于把连续数字切得太碎（手机号 11 位、身份证 18 位仍可能被切开，
#: 那是安全闸的重叠窗口要接住的事）。
MAX_CHUNK_CHARS = 16

#: 安全检查往前多看这么多个已放行的字符。
SAFETY_WINDOW_CHARS = 24


async def stream_display_chunks(text_stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """把 LLM 的 token 增量切成适合**显示**的小块，逐字保留原文。

    不 strip、不合并空白：换行和缩进是 markdown 的一部分，流式阶段丢掉它们
    会让列表在推送过程中糊成一行。
    """
    buffer = ""
    async for delta in text_stream:
        buffer += delta
        while True:
            cut = _cut_point(buffer)
            if cut is None:
                break
            yield buffer[:cut]
            buffer = buffer[cut:]
    if buffer:
        yield buffer


def _cut_point(buffer: str) -> int | None:
    """该在哪切。返回 None 表示还不到推送的时候。

    切到**第一个** flush 字符，不是最后一个。一次推到最后一个标点能少发几次
    事件，但会让整段文字变成一块——而轻量安全检查是按块替换的：同一块里有
    一个敏感词，整块都会被换成兜底话术。按句切时只有命中的那一句会被换，
    按"最后一个标点"切则可能把前面完全正常的几句一起吞掉（planner 的
    test_run_planner_turn_streaming_uses_joined_sentences_not_raw_text_when_substituted
    正是这么红的：一整段两句话在同一个 delta 里到达）。多发几次 SSE 事件的
    开销可以忽略，替换波及面不能。
    """
    first_punct = min(
        (i for i in (buffer.find(ch) for ch in DISPLAY_FLUSH_CHARS) if i >= 0),
        default=-1,
    )
    if first_punct >= 0:
        return first_punct + 1
    if len(buffer) >= MAX_CHUNK_CHARS:
        return MAX_CHUNK_CHARS
    return None


class LiteSafetyGate:
    """逐块放行前的轻量规则检查，带重叠窗口。

    有状态：它要记住已经放行的尾巴，才能在下一块到达时看见被切开的号码。
    每一轮回答用一个新实例——跨回答共用会让上一条回答的尾巴参与下一条的
    判定，那是没有根据的关联。
    """

    def __init__(self, banned_terms: list[str] | None = None) -> None:
        self._banned_terms = banned_terms
        self._tail = ""
        #: 有没有替换过。调用方据此决定能不能用原始文本重建完整回答。
        self.substituted = False

    def vet(self, chunk: str) -> str:
        """返回可以推给用户的文本：安全则原样，命中则换成兜底话术。"""
        result = check_text(self._tail + chunk, banned_terms=self._banned_terms, include_email=False)
        if not result.is_safe:
            self.substituted = True
            # 命中之后清空窗口：被拦下的那段没有放行，再拿它当上文去拼下一块，
            # 会让后面完全无辜的内容跟着一起被判不安全。
            self._tail = ""
            return LITE_SAFETY_FALLBACK_SENTENCE
        self._tail = (self._tail + chunk)[-SAFETY_WINDOW_CHARS:]
        return chunk
