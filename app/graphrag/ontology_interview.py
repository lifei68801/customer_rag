"""智能创建的访谈逻辑：把 LLM 的回复翻成骨架增量，合并进骨架，算问题清单的缺口。

三条硬规则（spec 行为规格 §1-2）：
1. 每个元素必须带 rationale，没带的丢弃——凭空出现的实体用户没法判断去留。
2. 名字不合规的那一条丢弃、其余保留——一条脏数据不该让整轮白问。
3. missing 由这里算，不信 LLM 自报——它看不到 review 状态，也容易顺着问题编。

LLM 调用的降级口径照 llm_extractor.py：超时/异常/非 JSON 一律"这一轮什么
都不加"，但要把原因交回界面说出来，不静默。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

from app.graphrag.ontology_categories import EXTRA_FIELD_NAME_PATTERN
from app.graphrag.value_types import EXTRA_FIELD_VALUE_TYPES
from app.providers.base import ProviderCapability, ProviderRequest

logger = logging.getLogger(__name__)

# 与 ontology_lifecycle._validate_draft_relation_type 同一条规则；那边是模块私有，
# 这里复制一份，两处要同步（取舍同 ontology_skills.py）。
_RELATION_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class TurnResult:
    question: str | None
    added: dict
    dropped: list[str] = field(default_factory=list)
    done: bool = False
    note: str | None = None


def _empty_added() -> dict:
    return {"term_types": [], "relation_types": [], "constraints": []}


def _stamp(item: dict, *, from_turn: int) -> dict:
    # 全都是猜的（spec 数据模型）：confidence 只有 guess 一个取值，留字段是为
    # 将来区分"用户明确说过"与"模型推的"。
    return {**item, "confidence": "guess", "from_turn": from_turn, "review": "pending"}


def _parse_extra_fields(raw: object, *, owner: str, dropped: list[str]) -> list[dict] | None:
    """字段有一个不合规就让整个实体作废：半个实体比没有更糟——ETL 会按声明
    的字段建索引，缺一个字段的实体落进去之后要靠人回头补。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        dropped.append(f"实体类型 {owner} 的 extra_fields 不是列表")
        return None
    fields: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            dropped.append(f"实体类型 {owner} 的某个字段不是映射")
            return None
        name = str(item.get("name", "")).strip()
        value_type = str(item.get("value_type", "")).strip()
        if not EXTRA_FIELD_NAME_PATTERN.match(name):
            dropped.append(f"实体类型 {owner} 的字段名 {name!r} 不合法（要 ASCII 标识符）")
            return None
        if value_type not in EXTRA_FIELD_VALUE_TYPES:
            dropped.append(f"实体类型 {owner} 的字段 {name} 的类型 {value_type!r} 不合法")
            return None
        fields.append({"name": name, "value_type": value_type, "label": str(item.get("label") or name)})
    return fields


def parse_turn_reply(text: str, *, from_turn: int) -> TurnResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return TurnResult(question=None, added=_empty_added(), note="这一轮没能从回答里认出新概念（模型没有按约定格式回复），可以换个说法再说一次。")
    if not isinstance(payload, dict):
        return TurnResult(question=None, added=_empty_added(), note="这一轮没能从回答里认出新概念（模型回复的形状不对），可以换个说法再说一次。")

    dropped: list[str] = []
    added = _empty_added()
    add = payload.get("add") if isinstance(payload.get("add"), dict) else {}

    for raw in add.get("term_types") or []:
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not value:
            continue
        if not rationale:
            dropped.append(f"实体类型 {value} 没有给出理由，丢弃")
            continue
        fields = _parse_extra_fields(raw.get("extra_fields"), owner=value, dropped=dropped)
        if fields is None:
            continue
        added["term_types"].append(_stamp({
            "value": value,
            "display_name": str(raw.get("display_name") or value),
            "rationale": rationale,
            "extra_fields": fields,
            "standard_name_value_type": "string",
        }, from_turn=from_turn))

    for raw in add.get("relation_types") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("relation_type", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not name:
            continue
        if not rationale:
            dropped.append(f"关系类型 {name} 没有给出理由，丢弃")
            continue
        if not _RELATION_TYPE_PATTERN.match(name):
            dropped.append(f"关系类型名 {name!r} 不合法（要大写字母开头的 A-Z0-9_），丢弃")
            continue
        added["relation_types"].append(_stamp({
            "relation_type": name,
            "example_phrase": str(raw.get("example_phrase") or ""),
            "description": str(raw.get("description") or ""),
            "rationale": rationale,
        }, from_turn=from_turn))

    for raw in add.get("constraints") or []:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject", "")).strip()
        relation = str(raw.get("relation", "")).strip()
        obj = str(raw.get("object", "")).strip()
        rationale = str(raw.get("rationale") or "").strip()
        if not (subject and relation and obj):
            continue
        if not rationale:
            dropped.append(f"约束 {subject}-{relation}-{obj} 没有给出理由，丢弃")
            continue
        added["constraints"].append(_stamp({
            "subject": subject, "relation": relation, "object": obj, "rationale": rationale,
        }, from_turn=from_turn))

    question_raw = payload.get("question")
    question = str(question_raw).strip() if isinstance(question_raw, str) and question_raw.strip() else None
    done = payload.get("done") is True
    return TurnResult(question=None if done else question, added=added, dropped=dropped, done=done)


def _merge_list(existing: list[dict], incoming: list[dict], key) -> list[dict]:
    """重名不重复加，只把新理由追加到已有那条上；review 保持用户的决定。"""
    by_key = {key(item): dict(item) for item in existing}
    order = [key(item) for item in existing]
    for item in incoming:
        k = key(item)
        if k in by_key:
            current = by_key[k]
            if item.get("rationale") and item["rationale"] not in (current.get("rationale") or ""):
                current["rationale"] = f"{current['rationale']}；{item['rationale']}" if current.get("rationale") else item["rationale"]
            continue
        by_key[k] = dict(item)
        order.append(k)
    return [by_key[k] for k in order]


def merge_additions(skeleton: dict, added: dict) -> dict:
    return {
        "term_types": _merge_list(skeleton.get("term_types", []), added.get("term_types", []), key=lambda t: t["value"]),
        "relation_types": _merge_list(skeleton.get("relation_types", []), added.get("relation_types", []), key=lambda r: r["relation_type"]),
        "constraints": _merge_list(
            skeleton.get("constraints", []), added.get("constraints", []),
            key=lambda c: (c["subject"], c["relation"], c["object"]),
        ),
    }


_INTERVIEW_SYSTEM = """你是企业知识图谱的本体建模顾问，正在访谈一位企业用户，目的是弄清他们的业务里有哪些**实体类型**（人、物、单据、组织、地点这类概念）、实体之间有哪些**关系类型**，以及每种关系连接哪两类实体（约束）。

每一轮：根据用户的最新回答，把你能确认的新概念加进骨架，然后问**一个**最有信息量的下一个问题。问题要具体、口语化、一次只问一件事。当你认为骨架已经足够描述他们的核心业务时，把 done 设为 true 并不再提问。

当前骨架（已经有的不要重复加）：
{skeleton}

只输出一个 JSON 对象，不要任何多余文字：
{{"question": "下一个问题或 null", "add": {{"term_types": [{{"value": "中文名", "display_name": "可选", "rationale": "为什么从回答里推出这个", "extra_fields": [{{"name": "ascii_name", "value_type": "string|number|integer|date", "label": "中文名"}}]}}], "relation_types": [{{"relation_type": "UPPER_SNAKE", "example_phrase": "一句话例子", "rationale": "..."}}], "constraints": [{{"subject": "实体类型", "relation": "关系类型", "object": "实体类型", "rationale": "..."}}]}}, "done": false}}

规则：每个元素必须有 rationale；relation_type 只能是大写字母、数字、下划线；extra_fields 的 name 只能是 ASCII 标识符。"""


async def ask_next(llm_registry, *, provider_name: str, turns: list[dict], skeleton: dict, timeout_sec: float = 60.0) -> TurnResult:
    """把全部历史 + 当前骨架交给模型，要它加元素并问下一个问题。

    from_turn 是**用户最新那条回答**在 turns 里的下标——元素出自那一轮，
    界面上点一下能跳回去看当时说了什么。
    """
    from_turn = max((i for i, t in enumerate(turns) if t.get("role") == "user"), default=len(turns) - 1)
    messages = [{"role": "system", "content": _INTERVIEW_SYSTEM.format(skeleton=json.dumps(skeleton, ensure_ascii=False))}]
    messages.extend({"role": t["role"], "content": t["text"]} for t in turns)
    try:
        result = await asyncio.wait_for(
            llm_registry.run(ProviderCapability.LLM, ProviderRequest(messages=messages), provider_name=provider_name),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.info("访谈这一轮超时")
        return TurnResult(question=None, added=_empty_added(), note=f"模型 {int(timeout_sec)} 秒内没有回复（超时），这一轮没有加任何概念。稍后再试，或换个说法。")
    except Exception:
        logger.warning("访谈这一轮调用失败", exc_info=True)
        return TurnResult(question=None, added=_empty_added(), note="模型调用失败，这一轮没有加任何概念。稍后再试。")
    return parse_turn_reply(result.text, from_turn=from_turn)


_NEEDS_SYSTEM = """给你一个企业用户想在知识图谱里问的业务问题，以及当前的本体骨架。判断要回答这个问题需要哪些实体类型和关系类型（用骨架里已有的名字；骨架里没有的用你认为合适的名字）。

当前骨架：
{skeleton}

只输出一个 JSON 对象：{{"needs": {{"term_types": ["..."], "relation_types": ["UPPER_SNAKE"]}}}}"""


async def infer_needs(llm_registry, *, provider_name: str, question: str, skeleton: dict, timeout_sec: float = 30.0) -> dict:
    empty = {"term_types": [], "relation_types": []}
    messages = [
        {"role": "system", "content": _NEEDS_SYSTEM.format(skeleton=json.dumps(skeleton, ensure_ascii=False))},
        {"role": "user", "content": question},
    ]
    try:
        result = await asyncio.wait_for(
            llm_registry.run(ProviderCapability.LLM, ProviderRequest(messages=messages), provider_name=provider_name),
            timeout=timeout_sec,
        )
        payload = json.loads(result.text)
    except asyncio.TimeoutError:
        logger.info("问题清单反推超时")
        return empty
    except json.JSONDecodeError:
        logger.warning("问题清单反推返回非 JSON")
        return empty
    except Exception:
        logger.warning("问题清单反推失败", exc_info=True)
        return empty
    needs = payload.get("needs") if isinstance(payload, dict) else None
    if not isinstance(needs, dict):
        return empty
    return {
        "term_types": [str(x).strip() for x in needs.get("term_types") or [] if str(x).strip()],
        "relation_types": [str(x).strip() for x in needs.get("relation_types") or [] if str(x).strip()],
    }


def compute_missing(needs: dict, skeleton: dict) -> list[str]:
    """问题需要、骨架里没有（或被拒了）的名字。拒过的也算缺：用户明确不要
    但问题需要，这正是要摆到他面前的矛盾，不能替他藏起来。"""
    have_terms = {t["value"] for t in skeleton.get("term_types", []) if t.get("review") != "rejected"}
    have_relations = {r["relation_type"] for r in skeleton.get("relation_types", []) if r.get("review") != "rejected"}
    missing = [name for name in needs.get("term_types", []) if name not in have_terms]
    missing += [name for name in needs.get("relation_types", []) if name not in have_relations]
    return missing
