import base64
import json

import httpx

from app.ingestion.dashscope_ocr import build_dashscope_ocr, dashscope_vision_ocr


def test_dashscope_vision_ocr_sends_image_and_prompt_returns_extracted_text(tmp_path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake-png-bytes")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "识别出的文字内容"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    text = dashscope_vision_ocr(
        image_path,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        client=client,
    )

    assert text == "识别出的文字内容"
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "qwen-vl-ocr"
    content_parts = body["messages"][0]["content"]
    image_part = next(p for p in content_parts if p["type"] == "image_url")
    expected_b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    assert image_part["image_url"]["url"] == f"data:image/png;base64,{expected_b64}"
    text_part = next(p for p in content_parts if p["type"] == "text")
    assert "文字" in text_part["text"]


def test_dashscope_vision_ocr_uses_custom_model_when_provided(tmp_path):
    image_path = tmp_path / "page.png"
    image_path.write_bytes(b"fake-png-bytes")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "文字"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    dashscope_vision_ocr(
        image_path,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="qwen-vl-ocr-latest",
        client=client,
    )

    assert captured["body"]["model"] == "qwen-vl-ocr-latest"


def test_build_dashscope_ocr_returns_ocr_function_with_config_baked_in(tmp_path):
    image_path = tmp_path / "page.jpg"
    image_path.write_bytes(b"fake-jpeg-bytes")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "文字"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ocr_fn = build_dashscope_ocr(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        client=client,
    )

    # 符合 OcrFunction 接口：只传 path 就能调用（base_url/api_key/client 已经固化）
    text = ocr_fn(image_path)

    assert text == "文字"
    assert captured["auth"] == "Bearer test-key"
    content_parts = captured["body"]["messages"][0]["content"]
    image_part = next(p for p in content_parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
