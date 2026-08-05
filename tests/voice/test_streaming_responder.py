from app.voice.streaming_responder import stream_sentences


async def _fake_stream(deltas: list[str]):
    for delta in deltas:
        yield delta


async def test_yields_complete_sentence_as_soon_as_punctuation_arrives():
    sentences = [
        s async for s in stream_sentences(_fake_stream(["你好", "，世界", "。", "再见"]))
    ]

    assert sentences == ["你好，世界。", "再见"]


async def test_yields_multiple_sentences_arriving_in_one_delta():
    sentences = [
        s async for s in stream_sentences(_fake_stream(["第一句。第二句！", "第三句"]))
    ]

    assert sentences == ["第一句。", "第二句！", "第三句"]


async def test_flushes_trailing_text_without_terminal_punctuation():
    sentences = [s async for s in stream_sentences(_fake_stream(["没有标点结尾"]))]

    assert sentences == ["没有标点结尾"]


async def test_empty_stream_yields_nothing():
    sentences = [s async for s in stream_sentences(_fake_stream([]))]

    assert sentences == []
