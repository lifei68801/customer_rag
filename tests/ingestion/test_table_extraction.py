import base64
import json
import time

import httpx
import pytest

from app.ingestion.table_extraction import (
    _parse_facts_json,
    build_table_extractor,
    extract_table_facts,
)


async def test_extract_table_facts_sends_image_and_prompt_returns_parsed_facts(tmp_path):
    image_path = tmp_path / "table_page_0.png"
    image_path.write_bytes(b"fake-png-bytes")
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '["教育程度类别：博士，数量（人）：818"]'}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    facts = await extract_table_facts(
        image_path,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        client=client,
    )

    assert facts == ["教育程度类别：博士，数量（人）：818"]
    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "qwen-vl-max"
    content_parts = body["messages"][0]["content"]
    image_part = next(p for p in content_parts if p["type"] == "image_url")
    expected_b64 = base64.b64encode(b"fake-png-bytes").decode("ascii")
    assert image_part["image_url"]["url"] == f"data:image/png;base64,{expected_b64}"
    text_part = next(p for p in content_parts if p["type"] == "text")
    assert "JSON 数组" in text_part["text"]


async def test_extract_table_facts_uses_custom_model_when_provided(tmp_path):
    image_path = tmp_path / "table_page_0.png"
    image_path.write_bytes(b"fake-png-bytes")
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await extract_table_facts(
        image_path,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="qwen3-vl-plus",
        client=client,
    )

    assert captured["body"]["model"] == "qwen3-vl-plus"


def test_parse_facts_json_strips_markdown_code_fence():
    raw = '```json\n["表格数据1", "表格数据2"]\n```'
    assert _parse_facts_json(raw) == ["表格数据1", "表格数据2"]


def test_parse_facts_json_handles_bare_json_array():
    raw = '["表格数据1", "表格数据2"]'
    assert _parse_facts_json(raw) == ["表格数据1", "表格数据2"]


def test_parse_facts_json_drops_blank_entries():
    raw = '["有效数据", "  ", ""]'
    assert _parse_facts_json(raw) == ["有效数据"]


def test_parse_facts_json_rejects_non_array_payload():
    with pytest.raises(ValueError):
        _parse_facts_json('{"not": "an array"}')


async def test_build_table_extractor_returns_function_with_config_baked_in(tmp_path):
    image_path = tmp_path / "table_page_0.png"
    image_path.write_bytes(b"fake-png-bytes")
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '["数据"]'}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = build_table_extractor(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        client=client,
    )

    facts = await extractor(image_path)

    assert facts == ["数据"]
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["model"] == "qwen-vl-max"


async def test_build_table_extractor_throttles_consecutive_calls(tmp_path):
    image_path = tmp_path / "table_page_0.png"
    image_path.write_bytes(b"fake-png-bytes")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = build_table_extractor(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        client=client,
    )

    start = time.monotonic()
    await extractor(image_path)
    await extractor(image_path)
    elapsed = time.monotonic() - start

    # _MIN_CALL_INTERVAL_SECONDS = 0.5：两次连续调用之间必须被拉开间隔，
    # 不能背靠背瞬间发出（防止触发服务端限流），允许少量调度抖动的下浮。
    assert elapsed >= 0.45
