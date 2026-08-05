from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, WebSocket
from pydantic import BaseModel

from app.api import deps
from app.graphrag.ontology import Term
from app.providers.asr import ASRProvider, ASRRequest
from app.providers.registry import ProviderRegistry
from app.voice.asr_term_correction import correct_asr_terms

router = APIRouter()


class ASRFinalizeResponse(BaseModel):
    text: str


@router.post("/voice/asr/finalize", response_model=ASRFinalizeResponse)
async def asr_finalize_endpoint(
    audio: UploadFile,
    asr_provider: ASRProvider | None = Depends(deps.get_asr_provider),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    terms: list[Term] = Depends(deps.get_terms),
) -> ASRFinalizeResponse:
    """对完整录音做一次全量二次识别 + 专有名词校正，输出进入 Agent 流程的最终文本。"""
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
) -> None:
    """流式 ASR：客户端按分片推送音频二进制，服务端逐片转写并回传增量文本。

    简化说明：架构文档设计的"分片去重合并+语气词过滤"（处理跨分片重叠、
    过滤"嗯""啊"等语气词）没有实现，这里只做最基础的"收到分片就转写、
    拼接所有分片文本作为最终结果"。真实生产环境下分片边界处的转写质量
    会明显差于完整语音的二次识别，这是需要在此基础上补的下一步。
    """
    await websocket.accept()
    if asr_provider is None:
        await websocket.send_json(
            {"type": "error", "message": "ASR provider 未配置"}
        )
        await websocket.close()
        return

    parts: list[str] = []
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        audio_bytes = message.get("bytes")
        if audio_bytes is not None:
            result = await asr_provider.transcribe(ASRRequest(audio_bytes=audio_bytes))
            text = result.text.strip()
            if text:
                parts.append(text)
                await websocket.send_json({"type": "partial", "text": text})
            continue

        text_message = message.get("text")
        if text_message == "stop":
            await websocket.send_json({"type": "final", "text": " ".join(parts)})
            return
