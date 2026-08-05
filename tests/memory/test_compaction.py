from app.memory.compaction import compact_messages, should_compact


def test_should_compact_returns_false_when_under_threshold():
    messages = [{"role": "user", "content": "你好"}]

    assert should_compact(messages, preserve_recent_messages=8) is False


def test_should_compact_returns_true_when_over_threshold():
    messages = [{"role": "user", "content": f"msg{i}"} for i in range(10)]

    assert should_compact(messages, preserve_recent_messages=8) is True


def test_compact_messages_keeps_recent_verbatim_and_summarizes_the_rest():
    messages = [{"role": "user", "content": f"msg{i}"} for i in range(10)]

    compacted = compact_messages(messages, preserve_recent_messages=4)

    assert compacted[0]["role"] == "system"
    assert "会话摘要" in compacted[0]["content"]
    assert compacted[1:] == messages[-4:]
