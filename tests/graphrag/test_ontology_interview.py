from __future__ import annotations

import asyncio
import json

import pytest

from app.graphrag.ontology_interview import (
    ask_next,
    compute_missing,
    infer_needs,
    merge_additions,
    parse_turn_reply,
)
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult

pytestmark = pytest.mark.anyio


class _FakeRegistry:
    """记下请求、按脚本回话。text=None 表示抛异常，delay 表示拖时间。"""

    def __init__(self, text: str | None, *, delay: float = 0.0) -> None:
        self.text = text
        self.delay = delay
        self.requests: list[ProviderRequest] = []

    async def run(self, capability, request, *, provider_name):
        assert capability is ProviderCapability.LLM
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.text is None:
            raise RuntimeError("provider down")
        return ProviderResult(text=self.text)


_EMPTY = {"term_types": [], "relation_types": [], "constraints": []}

_GOOD_REPLY = json.dumps({
    "question": "你们的商品有没有分品类？",
    "add": {
        "term_types": [
            {"value": "商品", "rationale": "用户说主要卖服装和家居",
             "extra_fields": [{"name": "price", "value_type": "number", "label": "价格"}]},
            {"value": "门店", "rationale": "用户提到线下渠道"},
        ],
        "relation_types": [
            {"relation_type": "SOLD_AT", "example_phrase": "某商品在某门店有售", "rationale": "商品通过门店卖"},
        ],
        "constraints": [
            {"subject": "商品", "relation": "SOLD_AT", "object": "门店", "rationale": "同上"},
        ],
    },
    "done": False,
}, ensure_ascii=False)


def test_parse_turn_reply_marks_everything_as_guess_with_provenance():
    result = parse_turn_reply(_GOOD_REPLY, from_turn=1)
    assert result.question == "你们的商品有没有分品类？"
    assert result.done is False
    sku = result.added["term_types"][0]
    assert sku["value"] == "商品"
    assert sku["confidence"] == "guess"
    assert sku["from_turn"] == 1
    assert sku["review"] == "pending"
    assert sku["rationale"] == "用户说主要卖服装和家居"
    assert sku["extra_fields"] == [{"name": "price", "value_type": "number", "label": "价格"}]
    assert result.added["relation_types"][0]["relation_type"] == "SOLD_AT"
    assert result.added["constraints"][0] == {
        "subject": "商品", "relation": "SOLD_AT", "object": "门店",
        "rationale": "同上", "confidence": "guess", "from_turn": 1, "review": "pending",
    }
    assert result.dropped == []


@pytest.mark.parametrize(
    "mutate,dropped_fragment,kept",
    [
        # 没有 rationale 的元素丢弃——凭空出现的实体用户没法判断去留
        (lambda d: d["add"]["term_types"][0].pop("rationale"), "商品", "门店"),
        # 关系类型名不合规（小写）丢弃，其余保留
        (lambda d: d["add"]["relation_types"][0].__setitem__("relation_type", "sold_at"), "sold_at", "商品"),
        # 字段名不合规：整个实体丢弃（半个实体比没有更糟——ETL 会按声明的字段建索引）
        (lambda d: d["add"]["term_types"][0]["extra_fields"][0].__setitem__("name", "价 格"), "商品", "门店"),
        # value_type 不合规同理
        (lambda d: d["add"]["term_types"][0]["extra_fields"][0].__setitem__("value_type", "blob"), "商品", "门店"),
    ],
)
def test_parse_turn_reply_drops_only_the_bad_element(mutate, dropped_fragment, kept):
    payload = json.loads(_GOOD_REPLY)
    mutate(payload)
    result = parse_turn_reply(json.dumps(payload, ensure_ascii=False), from_turn=1)
    assert any(dropped_fragment in reason for reason in result.dropped)
    names = [t["value"] for t in result.added["term_types"]] + [r["relation_type"] for r in result.added["relation_types"]]
    assert kept in names


def test_parse_turn_reply_non_json_adds_nothing_and_says_so():
    result = parse_turn_reply("我觉得你们需要一个商品实体", from_turn=1)
    assert result.added == _EMPTY
    assert result.question is None
    assert result.note is not None


def test_parse_turn_reply_done_flag_stops_asking():
    result = parse_turn_reply(json.dumps({"question": None, "add": _EMPTY, "done": True}), from_turn=3)
    assert result.done is True
    assert result.question is None


def test_merge_additions_dedupes_by_name_and_appends_rationale():
    skeleton = {
        "term_types": [{"value": "商品", "rationale": "第一轮说的", "confidence": "guess", "from_turn": 1, "review": "accepted", "extra_fields": []}],
        "relation_types": [],
        "constraints": [],
    }
    added = parse_turn_reply(_GOOD_REPLY, from_turn=3).added
    merged = merge_additions(skeleton, added)
    assert [t["value"] for t in merged["term_types"]] == ["商品", "门店"]
    # 同一个概念被两轮回答佐证：加强不是冲突，review 决定保持不变，理由追加
    assert merged["term_types"][0]["review"] == "accepted"
    assert "第一轮说的" in merged["term_types"][0]["rationale"]
    assert "用户说主要卖服装和家居" in merged["term_types"][0]["rationale"]
    # 不改入参
    assert skeleton["term_types"][0]["rationale"] == "第一轮说的"


def test_merge_additions_dedupes_constraints_by_triple():
    skeleton = {"term_types": [], "relation_types": [], "constraints": [
        {"subject": "商品", "relation": "SOLD_AT", "object": "门店", "rationale": "x", "confidence": "guess", "from_turn": 1, "review": "rejected"},
    ]}
    merged = merge_additions(skeleton, parse_turn_reply(_GOOD_REPLY, from_turn=2).added)
    assert len(merged["constraints"]) == 1
    assert merged["constraints"][0]["review"] == "rejected"


async def test_ask_next_sends_history_and_skeleton_and_parses_reply():
    registry = _FakeRegistry(_GOOD_REPLY)
    turns = [{"role": "assistant", "text": "先说说你们做什么？"}, {"role": "user", "text": "我们卖服装"}]
    result = await ask_next(registry, provider_name="p", turns=turns, skeleton=_EMPTY)
    assert result.question == "你们的商品有没有分品类？"
    assert result.added["term_types"][0]["from_turn"] == 1
    request = registry.requests[0]
    # 历史轮次原样进对话；骨架进 system prompt，模型才知道哪些已经有了
    assert request.messages[-1] == {"role": "user", "content": "我们卖服装"}
    assert "先说说你们做什么" in request.messages[1]["content"]
    assert "term_types" in request.messages[0]["content"]


async def test_ask_next_timeout_adds_nothing_and_explains():
    registry = _FakeRegistry(_GOOD_REPLY, delay=0.2)
    result = await ask_next(registry, provider_name="p", turns=[{"role": "user", "text": "x"}], skeleton=_EMPTY, timeout_sec=0.05)
    assert result.added == _EMPTY
    assert result.note is not None
    assert "超时" in result.note


async def test_ask_next_provider_failure_adds_nothing_and_explains():
    registry = _FakeRegistry(None)
    result = await ask_next(registry, provider_name="p", turns=[{"role": "user", "text": "x"}], skeleton=_EMPTY)
    assert result.added == _EMPTY
    assert result.note is not None


async def test_infer_needs_returns_names_and_falls_back_to_empty():
    good = _FakeRegistry(json.dumps({"needs": {"term_types": ["品类", "商品"], "relation_types": ["BELONGS_TO"]}}, ensure_ascii=False))
    assert await infer_needs(good, provider_name="p", question="哪个品类卖得最好", skeleton=_EMPTY) == {
        "term_types": ["品类", "商品"], "relation_types": ["BELONGS_TO"],
    }
    bad = _FakeRegistry("not json")
    assert await infer_needs(bad, provider_name="p", question="q", skeleton=_EMPTY) == {"term_types": [], "relation_types": []}


async def test_infer_needs_timeout_returns_empty_and_logs(caplog):
    caplog.set_level("INFO")
    registry = _FakeRegistry(
        json.dumps({"needs": {"term_types": ["品类"], "relation_types": []}}, ensure_ascii=False),
        delay=0.2,
    )
    result = await infer_needs(registry, provider_name="p", question="q", skeleton=_EMPTY, timeout_sec=0.05)
    assert result == {"term_types": [], "relation_types": []}
    assert "超时" in caplog.text


def test_compute_missing_ignores_rejected_and_is_computed_here_not_by_llm():
    skeleton = {
        "term_types": [{"value": "商品", "review": "accepted"}, {"value": "品类", "review": "rejected"}],
        "relation_types": [{"relation_type": "BELONGS_TO", "review": "pending"}],
        "constraints": [],
    }
    needs = {"term_types": ["品类", "商品", "门店"], "relation_types": ["BELONGS_TO", "SOLD_AT"]}
    # 拒过的品类算缺（用户明确不要，但问题需要——这正是要摆到他面前的矛盾）
    assert compute_missing(needs, skeleton) == ["品类", "门店", "SOLD_AT"]
