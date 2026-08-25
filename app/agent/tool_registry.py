from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import TermTypeCategory
from app.graphrag.ontology_constraints import AllowedCombination
from app.graphrag.term_guard import GraphClientProtocol
from app.providers.embedding import EmbeddingRegistry
from app.providers.registry import ProviderRegistry
from app.providers.rerank import RerankProvider
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import VectorRecord, VectorStore


class ToolManifestError(Exception):
    """manifest.yaml 格式错误，或 tool.py 没有导出 TOOL——启动阶段直接
    失败，不静默跳过（见 docs/superpowers/specs/2026-08-25-tool-plugin-
    architecture-design.md 的"错误处理"一节：一个工具的 schema 要参与
    function-calling 协议、最终字段还要过白名单校验，静默跳过一个格式
    错误的工具比启动直接失败更危险）。"""


class DuplicateToolNameError(Exception):
    """两个 manifest 声明了同一个 name。"""


@dataclass(frozen=True)
class ToolContext:
    """今天 _dispatch_tool_call/_resolve_tool_arguments 接收的那一整串
    参数（tenant_id、terms、graph_client、embedding_registry、
    llm_registry 等）收拢成一个数据类，工具实现按自己需要的取用，
    不需要的字段忽略。"""
    tenant_id: str
    question: str
    embedding_registry: EmbeddingRegistry
    embedding_provider_name: str
    vector_store: VectorStore
    bm25_index: BM25Index
    llm_registry: ProviderRegistry
    llm_provider_name: str
    rerank_provider: RerankProvider | None
    query_rewrite_enabled: bool
    terms: list[Term]
    graph_client: GraphClientProtocol | None
    confirmed_relation_types: set[str]
    term_type_schema: dict[str, TermTypeCategory]
    allowed_combinations: list[AllowedCombination]


class Tool(Protocol):
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        """把 ReAct 推理调用产出的原始参数，解析成真正用于执行的参数。
        没有额外解析步骤的工具直接返回 raw_arguments。"""
        ...

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> tuple[dict[str, Any], list[VectorRecord]]:
        """真正执行，返回 (喂给 LLM 的观察结果, 这次调用附带产生的检索记录)。

        第二个元素是为 vector_search_tool 这类需要把命中的 VectorRecord
        原样带出去（供 run_tool_calls 更新 retrieved_records/used_sources，
        这两者后续会被 responder/term_guard 等节点用到，不只是这次工具
        调用的展示内容）而存在的通道——没有原始检索记录可带的工具（比如
        structured_filter_query_tool）返回空列表。"""
        ...


@dataclass(frozen=True)
class ToolManifest:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    trigger_cue: str | None = None

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[Tool, ToolManifest]] = {}

    def register(self, manifest: ToolManifest, tool: Tool) -> None:
        if manifest.name in self._entries:
            raise DuplicateToolNameError(f"工具名重复: {manifest.name!r}")
        self._entries[manifest.name] = (tool, manifest)

    def get(self, name: str) -> tuple[Tool, ToolManifest]:
        return self._entries[name]

    def all(self) -> list[tuple[Tool, ToolManifest]]:
        return list(self._entries.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [manifest.to_schema() for _, manifest in self._entries.values()]

    def trigger_cues(self) -> list[str]:
        return [
            manifest.trigger_cue
            for _, manifest in self._entries.values()
            if manifest.trigger_cue
        ]


def _load_manifest(manifest_path: Path) -> ToolManifest:
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ToolManifestError(f"{manifest_path} 不是合法的 YAML: {exc}") from exc
    if not isinstance(raw, dict) or "name" not in raw or "parameters_schema" not in raw:
        raise ToolManifestError(f"{manifest_path} 缺少必填字段 name/parameters_schema")
    return ToolManifest(
        name=raw["name"],
        description=raw.get("description", ""),
        parameters_schema=raw["parameters_schema"],
        trigger_cue=raw.get("trigger_cue"),
    )


def _load_tool_module(tool_dir: Path):
    module_path = tool_dir / "tool.py"
    spec = importlib.util.spec_from_file_location(
        f"app.agent.tools.{tool_dir.name}.tool", module_path
    )
    if spec is None or spec.loader is None:
        raise ToolManifestError(f"无法加载 {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_tools(tools_dir: Path) -> ToolRegistry:
    """扫描 tools_dir/*/manifest.yaml，按 name 排序后依次导入对应
    tool.py、注册进一张新的 ToolRegistry。只在进程启动时调用一次
    （调用方负责做单例缓存，这个函数本身不缓存）。"""
    registry = ToolRegistry()
    manifest_paths = sorted(tools_dir.glob("*/manifest.yaml"))
    manifests = [(_load_manifest(p), p) for p in manifest_paths]
    manifests.sort(key=lambda item: item[0].name)
    for manifest, manifest_path in manifests:
        module = _load_tool_module(manifest_path.parent)
        if not hasattr(module, "TOOL"):
            raise ToolManifestError(f"{manifest_path.parent}/tool.py 没有导出 TOOL")
        registry.register(manifest, module.TOOL)
    return registry
