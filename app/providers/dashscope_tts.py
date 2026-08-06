from __future__ import annotations

import asyncio
from typing import Any, Callable

from app.providers.tts import TTSRequest, TTSResult


def _default_synthesizer_factory(*, model: str, voice: str) -> Any:
    from dashscope.audio.tts_v2 import SpeechSynthesizer

    return SpeechSynthesizer(model=model, voice=voice)


class DashScopeTTSProvider:
    """封装阿里百炼 CosyVoice 系列 TTS（dashscope SDK 的 tts_v2.
    SpeechSynthesizer），不是 OpenAI 兼容 REST 接口——实测过 CosyVoice
    只支持 WebSocket 协议，走的是私有部署的 base_url（不是默认的
    dashscope.aliyuncs.com），且这个部署上标准音色名（如"longwan"）
    不可用（报 InvalidParameter/引擎错误码418），必须先用
    dashscope.audio.tts_v2.VoiceEnrollmentService 克隆一个音色得到
    voice_id 才能合成。voice_id 的注册是一次性的管理操作，不在这个
    provider 的职责范围内，构造时传入一个已经注册好的 voice_id。

    SpeechSynthesizer.call() 是同步阻塞调用（内部维护一条 WebSocket
    连接），用 asyncio.to_thread 包一层以匹配 TTSProvider 协议的 async
    接口，不阻塞事件循环——和 MilvusVectorStore 包 pymilvus 同步客户端
    是同一个思路。

    dashscope 的鉴权/端点是模块级全局配置（dashscope.api_key/
    base_websocket_api_url/base_http_api_url），每次调用前都会重新设置
    一遍——如果同一进程内需要用不同的百炼账号/端点跑多个 TTS provider
    实例，这几个全局变量会互相覆盖，不是线程/并发安全的；这次场景下
    只有一套百炼账号，不构成实际问题，仅作为已知限制记录。
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        base_websocket_api_url: str,
        base_http_api_url: str,
        synthesizer_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._base_websocket_api_url = base_websocket_api_url
        self._base_http_api_url = base_http_api_url
        self._synthesizer_factory = synthesizer_factory or _default_synthesizer_factory

    def _call_sync(self, text: str) -> bytes:
        import dashscope

        dashscope.api_key = self._api_key
        dashscope.base_websocket_api_url = self._base_websocket_api_url
        dashscope.base_http_api_url = self._base_http_api_url

        synthesizer = self._synthesizer_factory(model=self._model, voice=self._voice)
        audio_data = synthesizer.call(text)
        if audio_data is None:
            raise RuntimeError(
                f"DashScope TTS 合成失败，响应详情: {synthesizer.get_response()}"
            )
        return audio_data

    async def synthesize(self, request: TTSRequest) -> TTSResult:
        audio_bytes = await asyncio.to_thread(self._call_sync, request.text)
        return TTSResult(audio_bytes=audio_bytes)
