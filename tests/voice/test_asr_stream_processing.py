from app.voice.asr_stream_processing import filter_filler_words, merge_chunk_transcript


def test_merge_chunk_transcript_appends_when_no_overlap():
    merged = merge_chunk_transcript("你好世界", "今天天气不错")

    assert merged == "你好世界今天天气不错"


def test_merge_chunk_transcript_removes_overlapping_tail():
    # 流式分片音频常有重叠窗口，导致相邻分片转写文本首尾重复
    committed = "我们讨论一下这个方案"
    new_chunk = "这个方案有三个优点"

    merged = merge_chunk_transcript(committed, new_chunk)

    assert merged == "我们讨论一下这个方案有三个优点"


def test_merge_chunk_transcript_handles_empty_committed():
    assert merge_chunk_transcript("", "你好") == "你好"


def test_merge_chunk_transcript_handles_empty_new_chunk():
    assert merge_chunk_transcript("你好", "") == "你好"


def test_filter_filler_words_removes_standalone_fillers():
    text = filter_filler_words("嗯我们讨论一下呃这个方案额")

    assert text == "我们讨论一下这个方案"


def test_filter_filler_words_keeps_meaningful_text_untouched():
    text = filter_filler_words("这个方案不错")

    assert text == "这个方案不错"
