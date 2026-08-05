from __future__ import annotations


def should_compact(
    messages: list[dict[str, str]], *, preserve_recent_messages: int
) -> bool:
    return len(messages) > preserve_recent_messages


def compact_messages(
    messages: list[dict[str, str]], *, preserve_recent_messages: int
) -> list[dict[str, str]]:
    """超出滑窗大小时，把较早的消息压缩为一条摘要 system 消息，近期消息保留原文。

    摘要是启发式统计（轮次数+各角色计数），不调用 LLM——保证压缩本身
    是确定性、零延迟、零成本的操作，不会因为 LLM 抖动影响主链路。
    """
    if not should_compact(messages, preserve_recent_messages=preserve_recent_messages):
        return messages

    keep_from = len(messages) - preserve_recent_messages
    old_messages = messages[:keep_from]
    recent_messages = messages[keep_from:]

    user_count = sum(1 for m in old_messages if m.get("role") == "user")
    assistant_count = sum(1 for m in old_messages if m.get("role") == "assistant")
    summary = (
        f"会话摘要：共压缩 {len(old_messages)} 条历史消息"
        f"（user={user_count}, assistant={assistant_count}）。"
    )
    summary_message = {"role": "system", "content": summary}
    return [summary_message, *recent_messages]
