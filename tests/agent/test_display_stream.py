from typing import AsyncIterator

import pytest

from app.agent.display_stream import (
    MAX_CHUNK_CHARS,
    LiteSafetyGate,
    stream_display_chunks,
)
from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE

pytestmark = pytest.mark.anyio


async def _deltas(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def _collect(*parts: str) -> list[str]:
    return [chunk async for chunk in stream_display_chunks(_deltas(*parts))]


async def test_pushes_at_secondary_punctuation_not_only_at_sentence_end():
    """长从句里一个句末标点都没有。只认句末标点的话，用户盯着空白直到整句落地。"""
    chunks = await _collect("这个知识库", "里有订单、", "客户，", "还有产品。")

    assert chunks == ["这个知识库里有订单、", "客户，", "还有产品。"]
    # 第一块攒了两个 delta 才等到标点，后两块各自到标点就推。


async def test_pushes_before_any_punctuation_shows_up():
    """一整段没有标点的文本（英文、代码、编号）也要流得动。"""
    chunks = await _collect("A" * (MAX_CHUNK_CHARS * 2 + 3))

    assert chunks[0] == "A" * MAX_CHUNK_CHARS
    assert "".join(chunks) == "A" * (MAX_CHUNK_CHARS * 2 + 3)


async def test_keeps_newlines_and_spacing_verbatim():
    """换行和缩进是 markdown 的一部分。stream_sentences 会 strip 掉它们，
    于是列表在推送过程中糊成一行——最终 final 事件带的是完整原文，所以
    只有流式那段时间里是坏的，恰恰是用户盯着看的那段。"""
    text = "订单如下：\n- 0-556-54422-6\n- 1-954013-64-7\n"

    chunks = await _collect(text)

    assert "".join(chunks) == text
    assert any(chunk.endswith("\n") for chunk in chunks)


async def test_output_is_exactly_the_input():
    """逐字保留：拼回来必须跟原文一模一样，一个字都不能多或少。"""
    parts = ("第一句。", "第二", "句，带逗号；", "还有\t制表符 和空格  ", "结尾没有标点")

    chunks = await _collect(*parts)

    assert "".join(chunks) == "".join(parts)


async def test_a_delta_carrying_several_punctuations_is_cut_at_each_of_them():
    """切到第一个标点，不是最后一个。

    一次推到最后一个标点能少发几次事件，但会让整段文字变成一块——而轻量
    安全检查按块替换：同一块里有一个敏感词，整块都会被换成兜底话术，前面
    完全正常的几句跟着一起没了。
    """
    chunks = await _collect("甲，乙，丙。")

    assert chunks == ["甲，", "乙，", "丙。"]


async def test_empty_stream_yields_nothing():
    assert await _collect() == []
    assert await _collect("") == []


# ── 带重叠窗口的轻量安全闸 ───────────────────────────────────────────────


def test_a_phone_number_split_across_chunks_is_still_caught():
    """切小之后，一个完整手机号可能被切成两块，两块各自都匹配不上正则。
    检查"已放行的尾巴 + 这一块"才抓得住——这是切小这件事直接引入的风险。"""
    gate = LiteSafetyGate()

    first = gate.vet("请拨打 1381")
    second = gate.vet("2345678 联系我们")

    assert first == "请拨打 1381"  # 半个号码不构成完整 PII，放行
    assert second == LITE_SAFETY_FALLBACK_SENTENCE
    assert gate.substituted is True


def test_a_phone_number_inside_one_chunk_is_caught():
    gate = LiteSafetyGate()

    assert gate.vet("电话 13812345678") == LITE_SAFETY_FALLBACK_SENTENCE


def test_normal_text_passes_through_verbatim():
    gate = LiteSafetyGate()

    assert gate.vet("订单 0-556-54422-6 的金额是 ") == "订单 0-556-54422-6 的金额是 "
    assert gate.vet("1200 元。") == "1200 元。"
    assert gate.substituted is False


def test_the_window_does_not_reach_back_forever():
    """窗口只看最近 24 个字。再往前拼的话，一段早就过去的数字会跟很久以后
    的另一段拼成一个"号码"，把无辜内容判成 PII。"""
    gate = LiteSafetyGate()

    gate.vet("1381")
    gate.vet("这里是一段完全无关的说明文字，足够长把窗口挤出去了。")
    assert gate.vet("2345678") == "2345678"


def test_the_window_is_cleared_after_a_substitution():
    """被拦下的那段没有放行，不能再拿它当上文——否则后面完全无辜的内容
    会跟着一起被判不安全。"""
    gate = LiteSafetyGate()

    assert gate.vet("电话 13812345678") == LITE_SAFETY_FALLBACK_SENTENCE
    assert gate.vet("其余内容照常显示") == "其余内容照常显示"


def test_banned_terms_still_apply():
    gate = LiteSafetyGate(banned_terms=["内部代号X"])

    assert gate.vet("这是内部代号X") == LITE_SAFETY_FALLBACK_SENTENCE
