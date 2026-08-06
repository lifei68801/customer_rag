from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, WebSocket
from pydantic import BaseModel

from app.api import deps
from app.config.settings import Settings
from app.graphrag.ontology import Term
from app.providers.asr import ASRProvider, ASRRequest
from app.providers.registry import ProviderRegistry
from app.voice.asr_stream_processing import filter_filler_words, merge_chunk_transcript
from app.voice.asr_term_correction import correct_asr_terms

router = APIRouter()


class ASRFinalizeResponse(BaseModel):
    text: str


@router.post("/voice/asr/finalize", response_model=ASRFinalizeResponse)
async def asr_finalize_endpoint(
    audio: UploadFile,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    tenant_id: str | None = Query(default=None),
    asr_provider: ASRProvider | None = Depends(deps.get_asr_provider),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    terms: list[Term] = Depends(deps.get_terms),
) -> ASRFinalizeResponse:
    """对完整录音做一次全量二次识别 + 专有名词校正，输出进入 Agent 流程的最终文本。

    该接口内部逻辑目前不按租户区分行为，这里调用 resolve_tenant_id() 单纯是
    为了建立与其他入口一致的身份校验边界（网关鉴权失败时拒绝请求），解析
    出来的 tenant_id 当前不需要在函数体其余部分使用。tenant_id 显式标注为
    Query(...)：因为该端点同时声明了 UploadFile 参数，请求会被当作
    multipart/form-data 处理，不显式标注的裸 str 参数在这种情况下会被
    FastAPI 当成表单字段而不是查询参数，与其他 HTTP 端点（tenant_id 走
    query/body）的取值方式不一致。
    """
    deps.resolve_tenant_id(gateway_tenant_id, tenant_id, source="asr_finalize")

    if asr_provider is None:
        raise HTTPException(status_code=503, detail="ASR provider 未配置")

    audio_bytes = await audio.read()
    result = await asr_provider.transcribe(ASRRequest(audio_bytes=audio_bytes))
    corrected = await correct_asr_terms(
        result.text,
        terms=terms,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
    )
    return ASRFinalizeResponse(text=corrected)


@router.websocket("/voice/asr/stream")
async def asr_stream_endpoint(
    websocket: WebSocket,
    asr_provider: ASRProvider | None = Depends(deps.get_asr_provider),
    settings: Settings = Depends(deps.get_settings),
) -> None:
    """流式 ASR：客户端按分片推送音频二进制，服务端逐片转写并回传增量文本。

    分片边界常有重叠音频窗口导致相邻分片转写文本首尾重复，用
    merge_chunk_transcript() 去重合并（而不是简单拼接/空格连接）；
    语气词过滤只在 stop 时对最终文本做一次（partial 阶段保留原始转写，
    优先保证增量反馈的响应速度，过滤放在最终定稿这一步）。

    网关鉴权校验放在 accept() 之后：get_gateway_tenant_id/resolve_tenant_id
    抛的是 HTTPException，WebSocket 协议不认这个类型（不会被 FastAPI 的
    异常处理中间件转换成关闭帧），这里手动调用（不用 Depends() 自动注入，
    而是当普通异步函数调用，参数从 websocket.headers/query_params 手动取）
    并用 try/except 转换成"发错误消息 + 关闭连接"，和上面 asr_provider 为
    None 时的处理方式保持一致。
    """
    await websocket.accept()
    if asr_provider is None:
        await websocket.send_json(
            {"type": "error", "message": "ASR provider 未配置"}
        )
        await websocket.close()
        return

    try:
        gateway_tenant_id = await deps.get_gateway_tenant_id(
            x_tenant_id=websocket.headers.get("x-tenant-id"),
            x_gateway_secret=websocket.headers.get("x-gateway-secret"),
            settings=settings,
        )
        deps.resolve_tenant_id(
            gateway_tenant_id,
            websocket.query_params.get("tenant_id"),
            source="asr_stream",
        )
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": exc.detail})
        await websocket.close()
        return

    committed = ""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        audio_bytes = message.get("bytes")
        if audio_bytes is not None:
            result = await asr_provider.transcribe(ASRRequest(audio_bytes=audio_bytes))
            chunk_text = result.text.strip()
            if chunk_text:
                merged = merge_chunk_transcript(committed, chunk_text)
                incremental = merged[len(committed) :]
                committed = merged
                if incremental:
                    await websocket.send_json({"type": "partial", "text": incremental})
            continue

        text_message = message.get("text")
        if text_message == "stop":
            await websocket.send_json(
                {"type": "final", "text": filter_filler_words(committed)}
            )
            return
