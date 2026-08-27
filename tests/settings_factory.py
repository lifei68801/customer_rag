"""测试专用的 Settings 构造 helper。

Settings 按领域拆成了独立的嵌套子模型（见 app/config/settings.py 顶部的
说明），每个子模型自己是一个 BaseSettings，直接 `SubSettings(field=value)`
构造时，没在调用里显式给出的字段会落到该子模型自己的类默认值——但如果不
传 `_env_file=None`，它仍然会去读本地开发者的 .env 文件，同一类"本地环境
静默覆盖测试预期"的问题（2026-08-27 全量测试跑排查到的根因，见
tests/api/test_agent_chat_routes.py 曾经的修复）在拆分之后，会在每一个子
模型上各自重演一次。

build_settings() 把这件事集中在一个地方做对：测试按熟悉的扁平字段名传参
（跟拆分前的写法一样，比如 `build_settings(gateway_shared_secret="x")`），
内部按前缀分发进正确的子模型，并统一带上 _env_file=None——调用方不需要
知道拆分后的嵌套结构，也不会重新踩到 .env 泄漏这个坑。
"""

from __future__ import annotations

from typing import Any

from app.config.settings import (
    AgentSettings,
    AsrSettings,
    EmbeddingSettings,
    GatewaySettings,
    IngestionSettings,
    LLMSettings,
    MemorySettings,
    MilvusSettings,
    Neo4jSettings,
    NeptuneSettings,
    OcrSettings,
    RerankSettings,
    Settings,
    SessionWindowSettings,
    TableExtractionSettings,
    TtsSettings,
)

# 顺序无所谓，但每个前缀必须是独立、互不为对方前缀的字符串（见
# app/config/settings.py 顶部关于"按字段现有前缀分组，不按业务领域分组"
# 的说明——这里的 key 集合就是那次分组的最终结果，两处必须保持一致）。
_SUBMODEL_CLASSES: dict[str, type] = {
    "llm": LLMSettings,
    "embedding": EmbeddingSettings,
    "milvus": MilvusSettings,
    "rerank": RerankSettings,
    "ocr": OcrSettings,
    "table_extraction": TableExtractionSettings,
    "ingestion": IngestionSettings,
    "neo4j": Neo4jSettings,
    "neptune": NeptuneSettings,
    "asr": AsrSettings,
    "tts": TtsSettings,
    "agent": AgentSettings,
    "memory": MemorySettings,
    "session_window": SessionWindowSettings,
    "gateway": GatewaySettings,
}

# 顶层字段（不属于任何子模型，见 Settings 类文档字符串里"字段名不共享
# 前缀，没有能对齐的分组"的说明）。
_TOP_LEVEL_FIELDS = frozenset(
    {
        "upload_dir",
        "graph_backend",
        "terminology_path",
        "graph_review_db_path",
        "redis_url",
        "admin_token",
        "banned_terms",
    }
)

_DEFAULT_KWARGS: dict[str, Any] = dict(
    llm_base_url="https://api.deepseek.com/v1",
    llm_api_key="k",
    llm_model="deepseek-chat",
    embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    embedding_api_key="k",
    embedding_model="text-embedding-v3",
    embedding_dimension=1024,
)


def _split_flat_kwargs(flat: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    top_level: dict[str, Any] = {}
    for key, value in flat.items():
        if key in _TOP_LEVEL_FIELDS:
            top_level[key] = value
            continue
        for prefix in _SUBMODEL_CLASSES:
            if key.startswith(prefix + "_"):
                grouped.setdefault(prefix, {})[key[len(prefix) + 1 :]] = value
                break
        else:
            raise ValueError(
                f"build_settings() 不认识字段 {key!r}——既不匹配任何子模型前缀"
                f"（{sorted(_SUBMODEL_CLASSES)}），也不在顶层字段列表"
                f"（{sorted(_TOP_LEVEL_FIELDS)}）里，检查是不是拼写错误。"
            )
    return grouped, top_level


def build_settings(**overrides: Any) -> Settings:
    """默认给出 LLM/embedding 三项必填配置，其余字段落到各自类默认值——
    与 Settings() 直接构造（不传任何字段）在语义上等价，只是补全了没有
    类默认值的必填项。传扁平字段名覆盖（如 `gateway_shared_secret="x"`、
    `agent_enable_autonomous_planning=True`），不需要知道嵌套结构。
    """
    flat = {**_DEFAULT_KWARGS, **overrides}
    grouped, top_level = _split_flat_kwargs(flat)
    submodels = {
        prefix: cls(_env_file=None, **grouped.get(prefix, {}))
        for prefix, cls in _SUBMODEL_CLASSES.items()
    }
    return Settings(_env_file=None, **submodels, **top_level)
