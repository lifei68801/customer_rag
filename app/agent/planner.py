from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from app.agent.tool_registry import ToolContext, ToolRegistry
from app.graphrag.term_guard import describe_association
from app.providers.base import ProviderCapability, ProviderRequest, ProviderStreamChunk, ToolCall
from app.providers.registry import ProviderRegistry
from app.retrieval.vector_store import VectorRecord
from app.safety.rules import LITE_SAFETY_FALLBACK_SENTENCE, check_text
from app.voice.streaming_responder import stream_sentences

logger = logging.getLogger(__name__)

_FINAL_ANSWER_INSTRUCTION = (
    "你已经达到本轮对话可用的工具调用次数上限，不能再调用任何工具了。"
    "请基于你目前已经查询到的全部信息，尽力给用户一个有帮助的回答：如果已经有"
    "明确的结论或数字，直接给出；如果现有信息不足以给出确定结论，清楚说明你"
    "目前掌握的情况、以及为什么无法进一步确认（比如某个维度在当前数据里没有"
    "区分度、或者查询本身没有找到匹配结果），不要用套话搪塞，也绝不能编造"
    "没有查到的数据。"
)


def _build_tool_call_round_result(
    messages: list[dict[str, Any]],
    answer_text: str,
    tool_calls: list[ToolCall],
) -> dict[str, Any]:
    """构造"这一轮模型请求了工具调用、且轮次预算还没耗尽"场景下的返回值：
    把 assistant 消息（带 tool_calls 字段）追加进对话历史，返回待执行的
    工具调用列表。run_planner_turn（非流式）和 run_planner_turn_streaming
    （流式）在这一步的逻辑完全一样，抽成这个共用函数，避免两处重复维护。

    轮次是否耗尽由调用方在调用这个函数之前就判断好——耗尽时调用方会转去
    调 _run_final_answer_attempt（或它的流式版本），根本不会走到这个
    函数，所以这个函数不再需要知道 round_num/max_tool_call_rounds。
    """
    messages = [
        *messages,
        {
            "role": "assistant",
            "content": answer_text or None,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ],
        },
    ]
    return {
        "planner_messages": messages,
        "pending_tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in tool_calls
        ],
    }


async def _run_final_answer_attempt(
    messages: list[dict[str, Any]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
) -> dict[str, Any]:
    """轮次预算耗尽、且这一轮 LLM 仍要求调用工具时的兜底：不带 tools 参数
    再调用一次 LLM，要求它基于已有信息给出最后的总结性回答，而不是直接
    放弃（今天的行为）。

    messages 是这一轮开始前的历史（不包含这次被拒绝的 tool_calls 请求，
    跟轮次未耗尽时"不能把申请了工具调用但没执行的 assistant 消息留在
    历史里"这条原则一致）。

    成功（拿到非空文本）：按跟"LLM 主动决定不再调工具、直接给出最终答案"
    完全一样的返回形状处理——调用方（route_after_planner）会把这当成
    正常完成一轮处理，流转到 planner_responder_node -> output_safety_node
    做完整的规则+语义安全审查，不会创建人工工单。

    失败（调用异常或返回空文本）：退回 {"planner_gave_up": True}，走今天
    完全一样的路径（fallback_node 静态文案 + create_ticket_node 创建
    工单）——这是这个函数"下限不比今天差"的保证。
    """
    final_messages = [*messages, {"role": "system", "content": _FINAL_ANSWER_INSTRUCTION}]
    try:
        result = await llm_registry.run(
            ProviderCapability.LLM,
            ProviderRequest(messages=final_messages),
            provider_name=llm_provider_name,
        )
    except Exception:
        logger.warning("_run_final_answer_attempt: 最后陈述调用失败", exc_info=True)
        return {"planner_gave_up": True}
    if not result.text:
        logger.warning("_run_final_answer_attempt: 最后陈述调用返回空文本")
        return {"planner_gave_up": True}
    messages = [*messages, {"role": "assistant", "content": result.text}]
    return {
        "planner_messages": messages,
        "answer_text": result.text,
        "planner_gave_up": False,
    }


async def run_planner_turn(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
    tool_registry: ToolRegistry,
) -> dict[str, Any]:
    """执行一轮 Planner 推理：调用 LLM，决定"再调工具"还是"给出最终答案"。

    round_num 语义是"已经完成的工具调用轮次"；只有当 LLM 在 round_num 已经
    达到上限时仍要求调用工具，才会转去 _run_final_answer_attempt 做最后
    一次总结性回答的尝试（成功就当正常完成，失败才真正放弃）——绝不在
    轮次耗尽后仍然执行它请求的工具，那样等于绕过了轮次上限。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    result = await llm_registry.run(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=tool_registry.schemas(), tool_choice="auto"),
        provider_name=llm_provider_name,
    )

    if result.tool_calls:
        if round_num >= max_tool_call_rounds:
            return await _run_final_answer_attempt(
                messages, llm_registry=llm_registry, llm_provider_name=llm_provider_name,
            )
        return _build_tool_call_round_result(messages, result.text, result.tool_calls)

    messages.append({"role": "assistant", "content": result.text})
    return {
        "planner_messages": messages,
        "answer_text": result.text,
        "planner_gave_up": False,
    }


async def _split_stream_text_and_tool_calls(
    raw_stream: AsyncIterator[ProviderStreamChunk],
    tool_calls_box: list[list[ToolCall] | None],
    raw_text_parts: list[str],
) -> AsyncIterator[str]:
    """把 provider 流拆成两路：文本增量原样 yield 出去供 stream_sentences()
    消费；工具调用（如果有）写进 tool_calls_box[0]，供调用方在这个生成器
    耗尽后读取——用长度为 1 的列表当"可写引用"，闭包不能直接对外层局部
    变量重新赋值。raw_text_parts 原样收集每个文本增量（不经过
    stream_sentences 的按句切分/strip），供调用方在没有发生安全替换时
    重建保留原始换行/空白的完整文本——stream_sentences 是为了流式切句
    展示用的，会丢掉句子之间的空白/换行，不适合用来重建 planner_messages
    里要喂回给模型的原文。
    """
    async for chunk in raw_stream:
        if chunk.text:
            raw_text_parts.append(chunk.text)
            yield chunk.text
        if chunk.tool_calls is not None:
            tool_calls_box[0] = chunk.tool_calls


async def _run_final_answer_attempt_streaming(
    messages: list[dict[str, Any]],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    banned_terms: list[str] | None,
    on_answer_chunk: Callable[[str], Awaitable[None]],
    streamed_round_texts: list[str],
) -> dict[str, Any]:
    """run_planner_turn_streaming 版本的轮次耗尽兜底：跟 _run_final_answer_
    attempt（非流式版本）语义一致，区别是这次调用同样走 stream_with_tools()
    （不传 tools，模型结构上不可能再请求工具调用）边生成边推送，跟主循环
    共用同一套逐句 check_text 安全替换逻辑，让用户看到的体验是从"查询
    过程"无缝过渡到"总结陈述"，而不是先看到一段查询叙述、中间断一下、
    再冒出一句不相关的静态兜底文案。

    不调用 on_tool_status()——这次不是"还在查"，是"在总结"，延续
    run_planner_turn_streaming 里同一条原则（见该函数文档字符串）。

    streamed_round_texts 是这一轮开始前已经流式展示过的所有轮次文本
    （含这一轮被拒绝前那句"让我查一下xxx"式的叙述，即使它没有被持久化
    进 planner_messages）——成功时把这次的总结文本也并进去，交给
    output_safety_node 做完整安全审查，跟正常轮次的处理方式完全一致。
    """
    final_messages = [*messages, {"role": "system", "content": _FINAL_ANSWER_INSTRUCTION}]
    sent_sentences: list[str] = []
    try:
        raw_stream = llm_registry.stream_with_tools(
            ProviderCapability.LLM,
            ProviderRequest(messages=final_messages),
            provider_name=llm_provider_name,
        )
        tool_calls_box: list[list[ToolCall] | None] = [None]
        raw_text_parts: list[str] = []
        text_stream = _split_stream_text_and_tool_calls(raw_stream, tool_calls_box, raw_text_parts)

        any_sentence_substituted = False
        async for sentence in stream_sentences(text_stream):
            safety_result = check_text(sentence, banned_terms=banned_terms, include_email=False)
            if safety_result.is_safe:
                safe_sentence = sentence
            else:
                safe_sentence = LITE_SAFETY_FALLBACK_SENTENCE
                any_sentence_substituted = True
            await on_answer_chunk(safe_sentence)
            sent_sentences.append(safe_sentence)
    except Exception:
        logger.warning(
            "_run_final_answer_attempt_streaming: 最后陈述流式调用中途失败，"
            "已经推送给用户的 %d 句话并入 streamed_round_texts 供后续安全审查",
            len(sent_sentences),
            exc_info=True,
        )
        return {
            "planner_gave_up": True,
            "streamed_round_texts": [*streamed_round_texts, *sent_sentences],
        }

    full_text = "".join(sent_sentences) if any_sentence_substituted else "".join(raw_text_parts)
    if not full_text:
        logger.warning("_run_final_answer_attempt_streaming: 最后陈述调用返回空文本")
        return {"planner_gave_up": True}

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
        "streamed_round_texts": [*streamed_round_texts, full_text],
    }


async def run_planner_turn_streaming(
    state: dict[str, Any],
    *,
    llm_registry: ProviderRegistry,
    llm_provider_name: str,
    max_tool_call_rounds: int,
    banned_terms: list[str] | None,
    on_answer_chunk: Callable[[str], Awaitable[None]],
    on_tool_status: Callable[[], Awaitable[None]],
    tool_registry: ToolRegistry,
) -> dict[str, Any]:
    """run_planner_turn 的流式版本：语义完全一致（同样的轮次上限检查、
    同样的 planner_messages 追加规则），区别只是这一轮的文本用
    stream_complete_with_tools() 边生成边推送，而不是一次性拿到完整
    文本。每句文本先过 check_text 轻量规则检查（跟确定性路径的
    responder_node 完全一致），命中就换成 LITE_SAFETY_FALLBACK_SENTENCE
    再推送。见 docs/superpowers/specs/2026-08-23-
    planner-streaming-typewriter-design.md。

    没有触发任何安全替换时，answer_text/回填进 planner_messages 的文本
    保留大模型输出的原始换行/空白格式（不经过 stream_sentences 的按句
    strip）；一旦某一句被安全替换过，则退回按句子拼接的版本，避免被
    过滤内容通过原始拼接重新进入 answer_text。

    on_tool_status 只在确认这一轮真的会继续执行工具调用（没有触发
    planner_gave_up）时才调用一次——轮次耗尽直接放弃的场景不应该让
    用户以为"还在查"。
    """
    messages = list(state.get("planner_messages", []))
    round_num = state.get("tool_call_round", 0)

    raw_stream = llm_registry.stream_with_tools(
        ProviderCapability.LLM,
        ProviderRequest(messages=messages, tools=tool_registry.schemas(), tool_choice="auto"),
        provider_name=llm_provider_name,
    )
    tool_calls_box: list[list[ToolCall] | None] = [None]
    raw_text_parts: list[str] = []
    text_stream = _split_stream_text_and_tool_calls(raw_stream, tool_calls_box, raw_text_parts)

    sent_sentences: list[str] = []
    any_sentence_substituted = False
    async for sentence in stream_sentences(text_stream):
        safety_result = check_text(sentence, banned_terms=banned_terms, include_email=False)
        if safety_result.is_safe:
            safe_sentence = sentence
        else:
            safe_sentence = LITE_SAFETY_FALLBACK_SENTENCE
            any_sentence_substituted = True
        await on_answer_chunk(safe_sentence)
        sent_sentences.append(safe_sentence)

    if any_sentence_substituted:
        # 至少一句被安全规则替换过——不能用原始拼接（会把被过滤的内容
        # 原样带回 answer_text/planner_messages，等于没过滤），退回按
        # 句子拼接的、已经做过安全替换的版本，跟当前展示给用户的内容
        # 保持一致。
        full_text = "".join(sent_sentences)
    else:
        # 没有任何一句被替换，用原始增量直接拼接，保留大模型输出的原始
        # 换行/空白格式（stream_sentences 为了切句会 strip 掉这些）。
        full_text = "".join(raw_text_parts)

    streamed_round_texts = state.get("streamed_round_texts", [])
    if full_text:
        streamed_round_texts = [*streamed_round_texts, full_text]

    tool_calls = tool_calls_box[0]
    if tool_calls:
        if round_num >= max_tool_call_rounds:
            return await _run_final_answer_attempt_streaming(
                messages,
                llm_registry=llm_registry,
                llm_provider_name=llm_provider_name,
                banned_terms=banned_terms,
                on_answer_chunk=on_answer_chunk,
                streamed_round_texts=streamed_round_texts,
            )
        result = _build_tool_call_round_result(messages, full_text, tool_calls)
        await on_tool_status()
        result["streamed_round_texts"] = streamed_round_texts
        return result

    messages = [*messages, {"role": "assistant", "content": full_text}]
    return {
        "planner_messages": messages,
        "answer_text": full_text,
        "planner_gave_up": False,
        "streamed_round_texts": streamed_round_texts,
    }


async def run_tool_calls(
    state: dict[str, Any],
    *,
    tool_registry: ToolRegistry,
    context: ToolContext,
) -> dict[str, Any]:
    """执行 state["pending_tool_calls"] 里的每一个工具调用，结果回填对话历史。

    每个工具的执行结果都会被追加为一条 role="tool" 消息（OpenAI 协议要求的
    格式），供下一轮 Planner 推理时看到；解析 arguments 失败、参数解析阶段
    失败都不抛异常，回填一条 {"error": ...} 观察结果，让 Planner 有机会
    自行调整重试。
    """
    messages = list(state.get("planner_messages", []))
    retrieved_records = list(state.get("retrieved_records", []))
    tool_results = list(state.get("tool_results", []))
    pending_calls = state.get("pending_tool_calls", [])

    async def _execute_one(call: dict[str, Any]) -> tuple[dict, list[VectorRecord]]:
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            content = json.dumps({"error": "arguments 不是合法 JSON"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        try:
            tool, _ = tool_registry.get(call["name"])
        except KeyError:
            content = json.dumps({"error": f"未知工具: {call['name']}"}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        try:
            resolved_arguments = await tool.resolve_arguments(arguments, context=context)
            observation, new_records = await tool.execute(resolved_arguments, context=context)
        except Exception as exc:
            # 优雅降级只对用户/LLM 可见的这一层负责——异常本身仍然要
            # 记日志，否则一个真实的代码 bug（不是预期内的域错误）会被
            # 悄悄吞成一句 LLM 看得懂但开发者永远不会注意到的 {"error": ...}，
            # 排查时无迹可寻。
            logger.warning(
                "run_tool_calls: 工具 %r 执行失败，降级为 error 观察结果",
                call["name"], exc_info=True,
            )
            content = json.dumps({"error": str(exc)}, ensure_ascii=False)
            return (
                {"tool_call_id": call["id"], "name": call["name"], "content": content},
                [],
            )
        if call["name"] == "structured_filter_query_tool":
            for anchor in observation.get("anchors", []):
                for neighbor in anchor.get("neighbors", []):
                    neighbor["association"] = describe_association(neighbor.get("hops", 1))
        content = json.dumps(observation, ensure_ascii=False)
        return (
            {"tool_call_id": call["id"], "name": call["name"], "content": content},
            new_records,
        )

    # 同一轮 LLM 可能同时请求多个工具（比如 vector_search_tool +
    # structured_filter_query_tool），彼此没有数据依赖——2026-08-10 起改成
    # asyncio.gather 并发执行，不再是 for 循环顺序 await。结果顺序按
    # pending_calls 原始顺序组装（asyncio.gather 保证返回顺序和传入协程
    # 顺序一致，不按完成先后），不因为改成并发就打乱 tool_call_id 对应
    # 关系的可读性。
    # 用 return_exceptions=True 等所有工具调用都跑完（不管成败）再决定要不要
    # 重新抛出，而不是用 gather 默认行为——默认行为下一个工具调用失败会让
    # gather 立刻返回，其它还在执行的工具调用变成没人处理的后台任务，可能
    # 引发不该发生的副作用，也可能在稍后失败时报一个不会被任何人处理的
    # "未获取异常"。这里等两边都落地再决定要不要抛，语义上仍然和串行版本
    # 一致：任一工具调用失败都让整个 run_tool_calls() 失败。
    outcomes = await asyncio.gather(
        *(_execute_one(call) for call in pending_calls), return_exceptions=True
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome

    for tool_result, new_records in outcomes:
        existing_ids = {r.id for r in retrieved_records}
        retrieved_records.extend(r for r in new_records if r.id not in existing_ids)
        tool_results.append(tool_result)
        messages.append({"role": "tool", "tool_call_id": tool_result["tool_call_id"], "content": tool_result["content"]})

    return {
        "planner_messages": messages,
        "pending_tool_calls": [],
        "retrieved_records": retrieved_records,
        "used_sources": [r.id for r in retrieved_records],
        "tool_results": tool_results,
        "tool_call_round": state.get("tool_call_round", 0) + 1,
    }


def route_after_planner(state: dict[str, Any]) -> str:
    """Planner 之后走 tool_call / responder / fallback 三选一。"""
    if state.get("planner_gave_up"):
        return "fallback"
    if state.get("pending_tool_calls"):
        return "tool_call"
    return "responder"
