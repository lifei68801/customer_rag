# Planner 工具插件化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **依赖前置计划**：这份计划假设 `docs/superpowers/plans/2026-08-25-progressive-disclosure-recall-augmented-params.md`（下称"计划2"）已经完整落地——`app/agent/tools.py` 里的 `STRUCTURED_FILTER_QUERY_USAGE_GUIDE`/`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA`/`STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`/`VECTOR_SEARCH_TOOL_SCHEMA` 都已经是计划2定义的最终形态，`app/agent/planner.py` 里的 `_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments`/`ToolArgumentResolutionError`/`_build_structured_filter_query_prompt`/`_strip_json_code_fence` 都已经按计划2实现并通过测试。这份计划**不重新设计**这些函数的行为，只是把它们从"硬编码在 `tools.py`/`planner.py` 里的两组特例"搬迁成"从目录动态发现的插件"——搬迁过程中逻辑不应该有任何变化，纯粹是代码组织方式的重构。开始执行前先确认计划2的三个提交（Task 1-3 的 commit）已经在当前分支历史里。

**Goal:** 把 `vector_search_tool`/`structured_filter_query_tool` 从硬编码在 `app/agent/tools.py`/`app/agent/planner.py` 里的两个特例，改造成从 `app/agent/tools/` 目录自动发现的插件——新增一个工具只需要新建一个目录（清单+实现），不需要改 `app/agent/planner.py` 里的任何分发代码。

**Architecture:** 新增 `app/agent/tool_registry.py` 定义 `Tool` 协议（`resolve_arguments`/`execute`）、`ToolContext`（收拢今天分散的一堆参数）、`ToolManifest`、`ToolRegistry`，以及启动时扫描 `app/agent/tools/*/manifest.yaml`+动态导入对应 `tool.py` 的 `discover_tools()`。删除 `app/agent/tools.py`（内容全部搬进 `app/agent/tools/vector_search/`、`app/agent/tools/structured_filter_query/` 两个目录），`app/agent/planner.py` 里原来的 `_TOOL_SCHEMAS`/`_dispatch_tool_call`/`_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments` 等函数被"查注册表、调用两个方法"取代。

**Tech Stack:** Python 3.12（`importlib.util` 动态导入、`pathlib`、`PyYAML`——已是项目依赖），pytest（含 `tmp_path` fixture 做临时目录测试）。

**Spec:** `docs/superpowers/specs/2026-08-25-tool-plugin-architecture-design.md`

## Global Constraints

- 发现机制必须是真正的自动发现（扫描 `app/agent/tools/*/manifest.yaml` + 动态导入），不是显式注册列表。
- manifest 文件格式为 YAML。
- 发现只在进程启动时跑一次（单例，双重检查锁定，模式照抄 `app/api/deps.py`），运行期间 `ToolRegistry` 的内容不再变化。
- 工具按 `manifest.yaml` 里的 `name` 字段排序后再依次导入/注册，不依赖文件系统扫描顺序。
- `tool.py` 必须在模块顶层导出一个叫 `TOOL` 的实例，满足 `Tool` 协议（`resolve_arguments`/`execute`）——不用反射/猜测类名的方式发现实现。
- manifest 格式错误、`tool.py` 缺 `TOOL` 导出、工具名重复——这三类问题必须在启动阶段让进程直接失败，不允许静默跳过。
- `Tool.resolve_arguments`/`Tool.execute` 的具体行为（召回逻辑、独立参数生成调用、matched_count 语义等）完全由计划2定义，这份计划只做代码搬迁，不改变行为。
- `tools` 请求参数（即 `registry.schemas()` 的返回值）在整个会话生命周期内必须保持字节级别一致——这份计划继续维持这个已有的 KV cache 硬约束，发现只在启动时跑一次正是这个约束的实现手段。

---

### Task 1: `Tool` 协议 + `ToolContext` + `ToolRegistry` + 发现机制

**Files:**
- Create: `app/agent/tool_registry.py`
- Test: `tests/agent/test_tool_registry.py`

**Interfaces:**
- Consumes：`Term`（`app/graphrag/ontology.py`）、`TermTypeCategory`（`app/graphrag/ontology_categories.py`）、`AllowedCombination`（`app/graphrag/ontology_constraints.py`）、`GraphClientProtocol`（`app/graphrag/term_guard.py`）、`EmbeddingRegistry`（`app/providers/embedding.py`）、`ProviderRegistry`（`app/providers/registry.py`）、`RerankProvider`（`app/providers/rerank.py`）、`BM25Index`（`app/retrieval/bm25.py`）、`VectorStore`（`app/retrieval/vector_store.py`）——这些都是今天 `app/agent/planner.py` 的 `_dispatch_tool_call`/`run_tool_calls` 已经在用的类型，`ToolContext` 只是把它们收拢成一个数据类，不引入新类型。
- Produces：`ToolContext`（dataclass）；`Tool`（Protocol，`resolve_arguments`/`execute` 两个 async 方法）；`ToolManifest`（dataclass，含 `to_schema()`）；`ToolRegistry`（`register`/`get`/`all`/`schemas`/`trigger_cues`）；`discover_tools(tools_dir: Path) -> ToolRegistry`；`ToolManifestError`/`DuplicateToolNameError`（两个异常类，供 Task 2/3 的 `tool.py` 间接触发，也供这个 Task 自己的测试直接断言）。

- [ ] **Step 1: 写失败的测试**

创建 `tests/agent/test_tool_registry.py`：

```python
import pytest

from app.agent.tool_registry import (
    DuplicateToolNameError,
    ToolManifestError,
    discover_tools,
)


def _write_tool(tools_dir, name: str, *, trigger_cue: str | None = None) -> None:
    tool_dir = tools_dir / name
    tool_dir.mkdir(parents=True)
    trigger_cue_line = f"trigger_cue: {trigger_cue!r}\n" if trigger_cue else ""
    (tool_dir / "manifest.yaml").write_text(
        f"name: {name}\n"
        f"description: \"{name} 的描述\"\n"
        f"{trigger_cue_line}"
        "parameters_schema:\n"
        "  type: object\n"
        "  properties:\n"
        "    query:\n"
        "      type: string\n"
        "  required: [query]\n",
        encoding="utf-8",
    )
    (tool_dir / "tool.py").write_text(
        "class _FakeTool:\n"
        "    async def resolve_arguments(self, raw_arguments, *, context):\n"
        "        return raw_arguments\n"
        "\n"
        "    async def execute(self, arguments, *, context):\n"
        "        return ({'ok': True, 'name': %r}, [])\n"
        "\n"
        "TOOL = _FakeTool()\n" % name,
        encoding="utf-8",
    )


def test_discover_tools_finds_and_registers_tools_sorted_by_name(tmp_path):
    _write_tool(tmp_path, "zzz_tool")
    _write_tool(tmp_path, "aaa_tool", trigger_cue="遇到aaa场景时使用")

    registry = discover_tools(tmp_path)

    names = [manifest.name for _, manifest in registry.all()]
    assert names == ["aaa_tool", "zzz_tool"]
    assert registry.trigger_cues() == ["遇到aaa场景时使用"]


async def test_discover_tools_registered_tool_resolve_and_execute_work(tmp_path):
    _write_tool(tmp_path, "echo_tool")
    registry = discover_tools(tmp_path)

    tool, manifest = registry.get("echo_tool")
    assert manifest.name == "echo_tool"
    resolved = await tool.resolve_arguments({"query": "x"}, context=None)
    assert resolved == {"query": "x"}
    result, records = await tool.execute(resolved, context=None)
    assert result == {"ok": True, "name": "echo_tool"}
    assert records == []


def test_discover_tools_schemas_shape():
    pass  # 见 Step 3b：验证 to_schema() 输出形状


def test_discover_tools_raises_on_duplicate_name(tmp_path):
    _write_tool(tmp_path, "dup_tool")
    # 两个不同目录名，manifest 里声明相同的 name。
    (tmp_path / "dup_tool_2").mkdir()
    (tmp_path / "dup_tool_2" / "manifest.yaml").write_text(
        "name: dup_tool\ndescription: \"重复\"\nparameters_schema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "dup_tool_2" / "tool.py").write_text(
        "class _T:\n"
        "    async def resolve_arguments(self, raw_arguments, *, context):\n"
        "        return raw_arguments\n"
        "    async def execute(self, arguments, *, context):\n"
        "        return ({}, [])\n"
        "TOOL = _T()\n",
        encoding="utf-8",
    )

    with pytest.raises(DuplicateToolNameError):
        discover_tools(tmp_path)


def test_discover_tools_raises_on_manifest_missing_required_field(tmp_path):
    tool_dir = tmp_path / "broken_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text("description: \"缺 name 字段\"\n", encoding="utf-8")
    (tool_dir / "tool.py").write_text("TOOL = None\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_discover_tools_raises_on_invalid_yaml(tmp_path):
    tool_dir = tmp_path / "bad_yaml_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (tool_dir / "tool.py").write_text("TOOL = None\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_discover_tools_raises_when_tool_py_missing_TOOL_export(tmp_path):
    tool_dir = tmp_path / "no_export_tool"
    tool_dir.mkdir()
    (tool_dir / "manifest.yaml").write_text(
        "name: no_export_tool\ndescription: \"x\"\nparameters_schema:\n  type: object\n  properties: {}\n",
        encoding="utf-8",
    )
    (tool_dir / "tool.py").write_text("# 没有定义 TOOL\n", encoding="utf-8")

    with pytest.raises(ToolManifestError):
        discover_tools(tmp_path)


def test_tool_manifest_to_schema_shape(tmp_path):
    _write_tool(tmp_path, "shape_tool")
    registry = discover_tools(tmp_path)
    _, manifest = registry.get("shape_tool")

    schema = manifest.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "shape_tool"
    assert schema["function"]["parameters"]["required"] == ["query"]


def test_registry_schemas_only_includes_registered_tools(tmp_path):
    _write_tool(tmp_path, "only_tool")
    registry = discover_tools(tmp_path)

    schemas = registry.schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "only_tool"
```

（删掉那个占位的 `test_discover_tools_schemas_shape`——是打草稿时留的名字冲突提醒，写实现前先去掉，不要真的留一个空 `pass` 测试进最终版本。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tool_registry.py -v`
Expected: 全部 FAIL（`ModuleNotFoundError: No module named 'app.agent.tool_registry'`）。

- [ ] **Step 3: 实现**

创建 `app/agent/tool_registry.py`：

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/test_tool_registry.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add app/agent/tool_registry.py tests/agent/test_tool_registry.py
git commit -m "feat(agent): add Tool protocol, ToolContext, and directory-scan tool discovery"
```

---

### Task 2: 把两个工具迁移进 `app/agent/tools/` 目录（删除 `app/agent/tools.py`）

**Files:**
- Delete: `app/agent/tools.py`
- Create: `app/agent/tools/vector_search/manifest.yaml`
- Create: `app/agent/tools/vector_search/tool.py`
- Create: `app/agent/tools/structured_filter_query/manifest.yaml`
- Create: `app/agent/tools/structured_filter_query/tool.py`
- Delete: `tests/agent/test_tools.py`
- Create: `tests/agent/tools/test_vector_search.py`
- Create: `tests/agent/tools/test_structured_filter_query.py`

**这一步为什么不能拆更小**：`app/agent/tools.py`（文件）和 `app/agent/tools/`（目录）在 Python import 系统里是同一个模块路径，不能共存——不存在"先加目录、`tools.py` 还留着"这种过渡态，两个工具的迁移必须在同一次改动里一起完成。

**Interfaces:**
- Consumes：Task 1 的 `Tool`/`ToolContext`；计划2产出的 `STRUCTURED_FILTER_QUERY_USAGE_GUIDE`/`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA`（内容原样保留，只是换了个文件存放——从 Python 字典/字符串常量变成 YAML 里的 `description`/`parameters_schema` 字段，以及 `tool.py` 里的一个字符串常量）、`_resolve_structured_filter_query_arguments`/`ToolArgumentResolutionError`/`_build_structured_filter_query_prompt`/`_strip_json_code_fence`（逻辑原样保留，从 `planner.py` 的模块级函数变成 `structured_filter_query/tool.py` 里的类方法/私有函数）。
- Produces：`app/agent/tools/vector_search/tool.py` 导出 `TOOL`（`resolve_arguments` 直通、`execute` 包装 `hybrid_search`）；`app/agent/tools/structured_filter_query/tool.py` 导出 `TOOL`（`resolve_arguments` 是计划2的召回增强逻辑、`execute` 包装 `run_structured_filter_query`）。

- [ ] **Step 1: 写失败的测试**

先删除 `tests/agent/test_tools.py`（它的测试全部迁移，见下）：

```bash
git rm tests/agent/test_tools.py
```

创建 `tests/agent/tools/__init__.py`（空文件，让这个目录成为可发现的测试包）：

```python
```

创建 `tests/agent/tools/test_vector_search.py`（内容照搬今天 `tests/agent/test_tools.py` 里 `test_vector_search_tool_returns_records_scoped_to_tenant`/`test_tool_schemas_do_not_expose_tenant_id` 两个测试关于 `vector_search_tool` 的部分，改成通过 `TOOL`/`manifest` 调用）：

```python
import yaml

from app.agent.tool_registry import ToolContext
from app.agent.tools.vector_search.tool import TOOL
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.providers.registry import ProviderRegistry
from app.retrieval.bm25 import BM25Index
from app.retrieval.vector_store import InMemoryVectorStore, VectorRecord


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[1.0, 0.0] for _ in request.texts])


class FakeLLMProvider:
    async def complete(self, request: ProviderRequest) -> ProviderResult:
        return ProviderResult(text="不应被用于查询改写")


def _manifest_path():
    import app.agent.tools.vector_search as pkg
    from pathlib import Path
    return Path(pkg.__file__).parent / "manifest.yaml"


def test_manifest_schema_does_not_expose_tenant_id():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert "tenant_id" not in raw["parameters_schema"]["properties"]


async def test_execute_returns_records_scoped_to_tenant():
    records = [
        VectorRecord(
            id="faq/network.md", vector=[1.0, 0.0], text="网络断开时请先重启路由器。",
            tenant_id="t1", metadata={},
        ),
        VectorRecord(
            id="faq/other-tenant.md", vector=[1.0, 0.0], text="属于别的租户的资料",
            tenant_id="t2", metadata={},
        ),
    ]
    vector_store = InMemoryVectorStore()
    await vector_store.upsert(records)
    bm25_index = BM25Index()
    bm25_index.index(records)

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    llm_registry = ProviderRegistry()
    llm_registry.register(ProviderCapability.LLM, "fake-llm", FakeLLMProvider())

    context = ToolContext(
        tenant_id="t1", question="网络连不上怎么办？",
        embedding_registry=embedding_registry, embedding_provider_name="fake-embedding",
        vector_store=vector_store, bm25_index=bm25_index,
        llm_registry=llm_registry, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=[], graph_client=None, confirmed_relation_types=set(),
        term_type_schema={}, allowed_combinations=[],
    )

    resolved = await TOOL.resolve_arguments({"query": "网络连不上怎么办？"}, context=context)
    observation, records = await TOOL.execute(resolved, context=context)

    assert [r["id"] for r in observation["results"]] == ["faq/network.md"]
    assert [r.id for r in records] == ["faq/network.md"]
```

创建 `tests/agent/tools/test_structured_filter_query.py`（照搬今天 `tests/agent/test_tools.py` 里 `test_structured_filter_query_tool_delegates_to_run_structured_filter_query`/`test_structured_filter_query_tool_resolves_name_anchor`/`test_structured_filter_query_tool_schema_only_exposes_query_intent`——最后这个是计划2 Task 2 已经改过的版本——三个测试，改成通过 `TOOL`/`manifest` 调用，`resolve_arguments` 部分额外加一个验证召回增强调用被正确触发的测试）：

```python
from pathlib import Path

import yaml

from app.agent.tool_registry import ToolContext
from app.agent.tools.structured_filter_query.tool import TOOL
from app.graphrag.ontology import Term
from app.graphrag.ontology_categories import ExtraFieldSpec, TermTypeCategory
from app.providers.base import ProviderCapability, ProviderRequest, ProviderResult
from app.providers.registry import ProviderRegistry


class ScriptedLLMProvider:
    def __init__(self, responses: list[ProviderResult]) -> None:
        self._responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def complete(self, request: ProviderRequest) -> ProviderResult:
        self.requests.append(request)
        return self._responses.pop(0)


def _manifest_path() -> Path:
    import app.agent.tools.structured_filter_query as pkg
    return Path(pkg.__file__).parent / "manifest.yaml"


def _base_context(*, terms=None, term_type_schema=None, graph_client=None,
                   confirmed_relation_types=None, allowed_combinations=None,
                   llm_registry=None) -> ToolContext:
    return ToolContext(
        tenant_id="t1", question="",
        embedding_registry=None, embedding_provider_name="",
        vector_store=None, bm25_index=None,
        llm_registry=llm_registry, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=terms or [], graph_client=graph_client,
        confirmed_relation_types=confirmed_relation_types or set(),
        term_type_schema=term_type_schema or {},
        allowed_combinations=allowed_combinations or [],
    )


def test_manifest_schema_only_exposes_query_intent():
    raw = yaml.safe_load(_manifest_path().read_text(encoding="utf-8"))
    assert set(raw["parameters_schema"]["properties"]) == {"query_intent"}
    for forbidden in ("anchor", "constraints", "hops", "matched_count"):
        assert forbidden not in raw["description"]


async def test_resolve_arguments_triggers_recall_augmented_call():
    llm_registry = ProviderRegistry()
    provider = ScriptedLLMProvider([ProviderResult(text='{"anchor": {"term_type": "订单号"}}')])
    llm_registry.register(ProviderCapability.LLM, "fake-llm", provider)
    context = _base_context(llm_registry=llm_registry)

    resolved = await TOOL.resolve_arguments({"query_intent": "查一下订单号有多少个"}, context=context)

    assert resolved == {"anchor": {"term_type": "订单号"}}
    assert provider.requests[0].tools is None


async def test_execute_delegates_to_run_structured_filter_query():
    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            return {"rows": [], "total_count": 0}

    context = _base_context(
        graph_client=_FakeGraphClient(),
        term_type_schema={"SKU": TermTypeCategory(
            value="SKU", extra_fields=[ExtraFieldSpec(name="numeric_value", value_type="number")],
        )},
    )

    result, records = await TOOL.execute(
        {"anchor": {"term_type": "SKU"},
         "constraints": [{"kind": "attribute", "field": "numeric_value", "operator": "gt", "value": 500}]},
        context=context,
    )

    assert result == {"matched_count": 0, "anchors": []}
    assert records == []


async def test_execute_resolves_name_anchor():
    terms = [Term(
        tenant_id="t1", node_key="示例错误码E502", standard_name="示例错误码E502",
        aliases=["网关超时示例"], term_type="error_code",
    )]

    class _FakeGraphClient:
        async def execute_structured_filter_query(self, args, *, resolved, tenant_id, term_type_schema):
            assert resolved.node_key == "示例错误码E502"
            return {"rows": [{
                "standard_name": "示例错误码E502", "node_key": "示例错误码E502",
                "term_type": "error_code", "all_properties": {},
            }], "total_count": 1}

    context = _base_context(
        terms=terms, graph_client=_FakeGraphClient(),
        term_type_schema={"error_code": TermTypeCategory(value="error_code", extra_fields=[])},
    )

    result, records = await TOOL.execute({"anchor": {"name": "网关超时示例"}}, context=context)

    assert result["matched_count"] == 1
    assert result["anchors"][0]["standard_name"] == "示例错误码E502"
    assert records == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/tools/ -v`
Expected: 全部 FAIL（`ModuleNotFoundError: No module named 'app.agent.tools.vector_search'` 等——目录/文件还没建）。

- [ ] **Step 3: 实现——先删 `app/agent/tools.py`，再建两个工具目录**

```bash
git rm app/agent/tools.py
```

创建 `app/agent/tools/__init__.py`（空文件，让 `app/agent/tools` 成为可导入的包）：

```python
```

创建 `app/agent/tools/vector_search/__init__.py`（空文件）：

```python
```

创建 `app/agent/tools/vector_search/manifest.yaml`：

```yaml
name: vector_search_tool
description: >
  在企业知识库中做混合检索（向量+关键词），返回相关文档片段。
  当需要补充事实性资料来回答用户问题时调用。
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: 检索查询语句，可以是用户问题本身或其改写/子问题
  required:
    - query
```

创建 `app/agent/tools/vector_search/tool.py`（`execute` 内容照搬今天 `app/agent/tools.py` 里 `vector_search_tool()` 函数体，`resolve_arguments` 直通——今天这个工具没有额外解析步骤）：

```python
from __future__ import annotations

from typing import Any

from app.agent.tool_registry import ToolContext
from app.retrieval.hybrid_search import hybrid_search
from app.retrieval.vector_store import VectorRecord


class VectorSearchTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        return raw_arguments

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> tuple[dict[str, Any], list[VectorRecord]]:
        """薄封装 hybrid_search，跟今天 app/agent/tools.py::vector_search_tool()
        逻辑一致——tenant_id 只能来自 context（由 tool_call_node 从
        AgentState 注入），不出现在 manifest.yaml 的 parameters_schema
        里，LLM 无法控制。原始 VectorRecord 列表随观察结果一起返回，供
        run_tool_calls 更新 retrieved_records/used_sources。"""
        query = str(arguments.get("query", ""))
        records = await hybrid_search(
            query,
            embedding_registry=context.embedding_registry,
            embedding_provider_name=context.embedding_provider_name,
            vector_store=context.vector_store,
            bm25_index=context.bm25_index,
            llm_registry=context.llm_registry,
            llm_provider_name=context.llm_provider_name,
            tenant_id=context.tenant_id,
            rerank_provider=context.rerank_provider,
            query_rewrite_enabled=context.query_rewrite_enabled,
            final_top_k=3,
        )
        observation = {"results": [{"id": r.id, "text": r.text} for r in records]}
        return observation, records


TOOL = VectorSearchTool()
```

创建 `app/agent/tools/structured_filter_query/__init__.py`（空文件）。

创建 `app/agent/tools/structured_filter_query/manifest.yaml`（`description`/`parameters_schema` 内容是计划2 Task 2 里 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA` 的最终版本，`trigger_cue` 是计划2 Task 2 收窄后并入 `_PLANNER_SYSTEM_PROMPT` 的那句话——这份计划把它挪回 manifest，由 Task 3 从 `registry.trigger_cues()` 拼进系统提示词）：

```yaml
name: structured_filter_query_tool
description: >
  在知识图谱里查询实体数量/满足条件的实体列表——用自然语言描述你想查什么就行，
  不需要给出结构化参数，后续步骤会引导你把它转成实际能执行的查询。
trigger_cue: >
  看到「多少个」「数量」等计数意图时，应该用 structured_filter_query_tool 给出确定数字，
  不能仅凭检索到的文档片段猜测，也不能因为一次调用没查到就直接放弃。
parameters_schema:
  type: object
  properties:
    query_intent:
      type: string
      description: >
        用自然语言描述这次想查询/筛选的内容：想找什么类型的实体、有什么筛选条件、
        涉及哪些已知的名字。写得越具体、越自包含（把"它""这个"之类的指代词换成
        前面已经了解到的具体名字）越好——这句话会被用来检索本体里相关的术语和
        关系作为参考，帮你把接下来的实际查询参数填对。
  required:
    - query_intent
```

创建 `app/agent/tools/structured_filter_query/tool.py`（`resolve_arguments` 内容照搬计划2的 `_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments`/`_build_structured_filter_query_prompt`/`_strip_json_code_fence`/`ToolArgumentResolutionError`，`execute` 内容照搬今天 `app/agent/tools.py` 里 `structured_filter_query_tool()` 函数体；`STRUCTURED_FILTER_QUERY_USAGE_GUIDE`/`STRUCTURED_FILTER_QUERY_PARAMETERS_SCHEMA` 这两个计划2引入的模块级常量，内容原样搬进这个文件当模块级私有常量）：

```python
from __future__ import annotations

import json
from typing import Any

from app.agent.tool_registry import ToolContext
from app.graphrag.ontology_recall import format_recall_candidates, recall_ontology_candidates
from app.graphrag.structured_filter_query import run_structured_filter_query
from app.providers.base import ProviderCapability, ProviderRequest
from app.retrieval.vector_store import VectorRecord

_USAGE_GUIDE = (
    "在知识图谱里查询实体——支持三种用法，可以组合使用：\n"
    "1. 已知实体名，查它是什么/关联着什么：anchor.name（会做别名模糊匹配）+ expand。\n"
    "2. 不知道具体实体名，按条件筛选一批满足条件的实体，"
    "适用于「有没有xx以上的」「比xx大的有哪些」「xx类目下有没有yy」"
    "「xx有多少个/数量是多少」这类问题：anchor.term_type + constraints。\n"
    "3. 上述两种可以叠加 expand，展开命中锚点的邻居关系。\n"
    "看到「多少个/数量」等计数意图时，必须以 anchor.term_type + constraints 模式返回的 "
    "matched_count 为准给出确定数字（anchor.name 模式的 matched_count 只表示"
    "「是否找到了这个实体」，是 0 或 1，不是数量答案）——不能仅凭检索到的文档片段或邻居关系"
    "列表猜测。constraints 里 standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
    "不要求填精确的标准名称。"
)

_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "anchor": {
            "type": "object",
            "description": "起点定位方式，二选一",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "已知的实体名称或别名"},
                        "type_hint": {
                            "type": "string",
                            "description": "该实体的类型（可选，同名实体存在多个类型时用于消歧）",
                        },
                    },
                    "required": ["name"],
                },
                {
                    "type": "object",
                    "properties": {
                        "term_type": {
                            "type": "string",
                            "description": "要筛选的实体类型（如 SKU、Product、Category），结果就是这个类型的实体列表",
                        },
                    },
                    "required": ["term_type"],
                },
            ],
        },
        "constraints": {
            "type": "array",
            "description": "过滤条件列表，条件之间是 AND 关系，可以为空（anchor.name 模式下留空表示不额外过滤，"
                           "直接用解析出的锚点）。standard_name 字段的 eq/ne 比较值支持别名/模糊匹配，"
                           "不要求填精确的标准名称——比如用户说的口语化名字可以直接填进来，系统会自动解析。",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["attribute", "relation"],
                        "description": "attribute：直接比较锚点实体自己的字段；relation：经过关系跳到目标实体再比较",
                    },
                    "field": {
                        "type": "string",
                        "description": "kind=attribute 时必填：要比较的字段名（standard_name 或该实体类型已声明的属性字段名）",
                    },
                    "operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "比较运算符，实际可用范围取决于字段类型",
                    },
                    "value": {"description": "kind=attribute 时必填：比较的目标值"},
                    "hops": {
                        "type": "array",
                        "description": "kind=relation 时必填：从锚点出发的关系跳数组，最多2跳",
                        "items": {
                            "type": "object",
                            "properties": {
                                "relation_type": {"type": "string", "description": "关系类型，如 HAS_VARIANT"},
                                "direction": {"type": "string", "enum": ["outgoing", "incoming"]},
                                "target_term_type": {"type": "string", "description": "这一跳到达的实体类型"},
                            },
                            "required": ["relation_type", "direction", "target_term_type"],
                        },
                    },
                    "target_field": {
                        "type": "string",
                        "description": "kind=relation 时必填：在最后一跳到达的实体上比较哪个字段",
                    },
                    "target_operator": {
                        "type": "string",
                        "enum": ["gt", "gte", "lt", "lte", "eq", "ne", "starts_with",
                                 "all_lte", "all_gte", "any_lte", "any_gte"],
                        "description": "kind=relation 时必填：对 target_field 用的运算符",
                    },
                    "target_value": {"description": "kind=relation 时必填：比较的目标值"},
                },
                "required": ["kind"],
            },
        },
        "expand": {
            "type": ["object", "null"],
            "description": "可选：展开命中锚点的邻居关系",
            "properties": {
                "hops": {"type": "integer", "enum": [1, 2], "description": "展开几跳，默认1"},
                "relation_type": {
                    "type": ["string", "null"],
                    "description": "只展开这种关系类型；不传或传 null 表示任意类型",
                },
                "direction": {
                    "type": "string",
                    "enum": ["outgoing", "incoming", "both"],
                    "description": "关系方向，默认 both",
                },
            },
        },
        "group_by": {
            "type": ["object", "null"],
            "description": "可选：按某个字段做 distinct 值统计而不是返回实体列表本身",
            "properties": {
                "constraint_index": {
                    "type": "integer",
                    "description": "指向 constraints 数组里某个 kind=relation 约束的下标，按它的 target_field 分组",
                },
            },
        },
        "limit": {
            "type": "integer",
            "description": "返回结果的最大条数，默认20——预期命中数量较多时"
                           "（如宽泛的数值区间过滤），请设置一个合理的值避免返回过多结果",
        },
    },
    "required": ["anchor"],
}


class ToolArgumentResolutionError(Exception):
    """resolve_arguments 失败时抛出——调用方（app/agent/planner.py 的
    run_tool_calls）捕获后降级成这次工具调用的 {"error": ...} 观察结果，
    不会让整个 Planner 轮次崩溃。"""


def _strip_json_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped[3:]
        if stripped.startswith("json"):
            stripped = stripped[4:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    return stripped


def _build_prompt(query_intent: str, candidates) -> str:
    schema_text = json.dumps(_PARAMETERS_SCHEMA, ensure_ascii=False, indent=2)
    return (
        "你是一个把自然语言查询意图转成结构化查询参数的助手。给定下面的查询意图、"
        "使用说明、JSON Schema、以及召回到的本体候选参考，输出一段严格匹配这个 "
        "JSON Schema 的 JSON 对象作为你的完整回复——不要输出任何 JSON 之外的文字，"
        "也不要用 markdown 代码块包裹。\n\n"
        f"使用说明：\n{_USAGE_GUIDE}\n\n"
        f"JSON Schema：\n{schema_text}\n\n"
        "constraints.hops 里的 relation_type/target_term_type、constraints 里的 "
        "field/target_field，以及 anchor.term_type，都应该优先使用下面候选参考里"
        "出现过的名字，不要凭空发明没见过的名字。\n\n"
        f"候选参考：\n{format_recall_candidates(candidates)}\n\n"
        f"查询意图：{query_intent}"
    )


class StructuredFilterQueryTool:
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        query_intent = str(raw_arguments.get("query_intent") or "").strip() or context.question
        candidates = recall_ontology_candidates(
            query_intent, terms=context.terms, term_type_schema=context.term_type_schema,
            allowed_combinations=context.allowed_combinations,
        )
        prompt = _build_prompt(query_intent, candidates)
        try:
            result = await context.llm_registry.run(
                ProviderCapability.LLM,
                ProviderRequest(messages=[{"role": "user", "content": prompt}]),
                provider_name=context.llm_provider_name,
            )
        except Exception as exc:
            raise ToolArgumentResolutionError(f"参数生成调用失败：{exc}") from exc
        try:
            return json.loads(_strip_json_code_fence(result.text))
        except json.JSONDecodeError as exc:
            raise ToolArgumentResolutionError(
                f"参数生成调用返回的内容不是合法 JSON：{result.text[:200]!r}"
            ) from exc

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> tuple[dict[str, Any], list[VectorRecord]]:
        if context.graph_client is None:
            return {"error": "structured_filter_query_tool 未配置"}, []
        observation = await run_structured_filter_query(
            arguments, terms=context.terms, graph_client=context.graph_client,
            tenant_id=context.tenant_id,
            confirmed_relation_types=context.confirmed_relation_types,
            term_type_schema=context.term_type_schema,
        )
        return observation, []


TOOL = StructuredFilterQueryTool()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/tools/ -v`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add app/agent/tools/ tests/agent/tools/
git rm app/agent/tools.py tests/agent/test_tools.py
git commit -m "refactor(agent): migrate vector_search_tool/structured_filter_query_tool into plugin directories"
```

（`app/agent/tools.py`/`tests/agent/test_tools.py` 已经在 Step 1/3 用 `git rm` 标记删除，这里的 `git add`/`git rm` 是把所有变更一起提交——如果 Step 1/3 已经把删除操作 `git add` 过，这里的 `git rm` 会提示"文件已经不在工作区"，属于正常情况，用 `git status` 确认这两个文件确实处于"deleted"暂存状态即可。）

---

### Task 3: 接入注册表，替换 `planner.py` 里硬编码的分发代码

**Files:**
- Modify: `app/agent/planner.py`（删除 `_TOOL_SCHEMAS`/`_dispatch_tool_call`/`_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments`/`_build_structured_filter_query_prompt`/`_strip_json_code_fence`/`ToolArgumentResolutionError`，`run_tool_calls`/`run_planner_turn`/`run_planner_turn_streaming` 改用注册表）
- Modify: `app/agent/graph.py`（`build_agent_graph` 新增 `tool_registry` 参数，`_PLANNER_SYSTEM_PROMPT` 的触发线索从 `tool_registry.trigger_cues()` 拼接）
- Modify: `app/api/deps.py`（新增 `get_tool_registry` 单例依赖，模式照抄 `get_graph_client`/`get_bm25_index`）
- Modify: `app/api/agent_routes.py`（用 `deps.get_tool_registry` 依赖注入，传给 `build_agent_graph`）
- Test: `tests/agent/test_planner.py`（大量测试需要改成通过 fake `ToolRegistry`/`ToolContext` 驱动，而不是直接传一堆分散参数——这是这个 Task 里工作量最大的一步）

**Interfaces:**
- Consumes：Task 1 的 `ToolRegistry`/`ToolContext`/`discover_tools`；Task 2 迁移后的两个工具目录。
- Produces：`run_tool_calls(state, *, tool_registry: ToolRegistry, context: ToolContext) -> dict[str, Any]`（签名大幅简化——今天分散的十几个关键字参数收拢成 `context` 一个）；`run_planner_turn`/`run_planner_turn_streaming` 的 `tools` 参数改成 `tool_registry.schemas()`。

- [ ] **Step 1: 读一遍 `app/agent/planner.py` 当前内容（计划2落地后的状态），确认改动范围**

这一步没有代码要写——先把 `app/agent/planner.py` 完整读一遍，标出：`_TOOL_SCHEMAS`（模块顶部）、`_dispatch_tool_call`、`_resolve_tool_arguments`、`_resolve_structured_filter_query_arguments`、`_build_structured_filter_query_prompt`、`_strip_json_code_fence`、`ToolArgumentResolutionError`、`run_tool_calls`（含内部的 `_execute_one`）、`run_planner_turn`、`run_planner_turn_streaming` 这几处的准确行号——这些行号在计划2执行完之后会跟这份计划文档写的不完全一致（工具会插入的位置取决于计划2的实现细节），后续步骤按"函数名"定位而不是按行号定位，实现时以读到的真实内容为准。

- [ ] **Step 2: 写失败的测试——用新签名重写 `run_tool_calls` 相关测试**

`tests/agent/test_planner.py` 里所有直接调用 `run_tool_calls`/`_dispatch_tool_call`/`_resolve_tool_arguments` 的测试，都要改成基于 `ToolRegistry`/`ToolContext` 的写法。新增一个共享的测试辅助函数（放在文件顶部，靠近其它 fixture）：

```python
def _fake_tool_registry(**tools) -> "ToolRegistry":
    """tools 是 name -> Tool 实例的映射，直接注册进一个真实的 ToolRegistry
    （复用真实类而不是再造一个 fake registry），manifest 用最简单的占位
    内容，测试不关心 schema 具体形状时用这个够了。"""
    from app.agent.tool_registry import ToolManifest, ToolRegistry

    registry = ToolRegistry()
    for name, tool in tools.items():
        registry.register(
            ToolManifest(name=name, description="", parameters_schema={"type": "object", "properties": {}}),
            tool,
        )
    return registry


def _context(**overrides) -> "ToolContext":
    from app.agent.tool_registry import ToolContext

    defaults = dict(
        tenant_id="t1", question="",
        embedding_registry=None, embedding_provider_name="",
        vector_store=None, bm25_index=None,
        llm_registry=None, llm_provider_name="fake-llm",
        rerank_provider=None, query_rewrite_enabled=False,
        terms=[], graph_client=None, confirmed_relation_types=set(),
        term_type_schema={}, allowed_combinations=[],
    )
    defaults.update(overrides)
    return ToolContext(**defaults)
```

把 `test_run_tool_calls_executes_vector_search_and_scopes_to_state_tenant`（读一下这个测试现在的完整内容，因为它在计划2执行期间没有被改动过，行号仍然是今天的第 171-212 行附近）改造成：用真实的 `app.agent.tools.vector_search.tool.TOOL` 注册进 `_fake_tool_registry(vector_search_tool=TOOL)`，`run_tool_calls(state, tool_registry=registry, context=_context(tenant_id="t1", embedding_registry=..., ...))` 这种调用形式。其余 `test_run_tool_calls_*`/`test_dispatch_tool_call_*` 系列测试同理改写——`_dispatch_tool_call` 这个函数本身被删除了，原来直接测它的两个测试（`test_dispatch_tool_call_routes_structured_filter_query_tool`/`test_dispatch_tool_call_reports_unconfigured_when_schema_data_missing`）删掉，因为它们测的是已经不存在的分发逻辑——等价的行为已经被 Task 2 `tests/agent/tools/test_structured_filter_query.py` 里的 `test_execute_delegates_to_run_structured_filter_query` 系列覆盖。

`test_resolve_tool_arguments_*` 系列（计划2 Task 3 新增的那批）同理删除——等价行为已经被 Task 2 `tests/agent/tools/test_structured_filter_query.py` 里的 `test_resolve_arguments_triggers_recall_augmented_call` 覆盖。

`test_route_after_planner_*` 系列不受影响（不涉及 `run_tool_calls`/`_dispatch_tool_call`）。

- [ ] **Step 3: 实现——`planner.py` 改用注册表**

`run_tool_calls` 改成：

```python
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
```

**关于 `VectorRecord` 透传的设计决定（pre-flight 阶段已拍板，不再是 Task 3 实现时才决定的开放问题）**：`vector_search_tool` 需要把命中的原始 `VectorRecord` 列表带出来，供 `run_tool_calls` 更新 `retrieved_records`/`used_sources`（这两者后续会被 `responder`/`term_guard` 等节点用到，不只是这次工具调用的展示内容），而 `structured_filter_query_tool` 没有这个需要。`Tool.execute` 协议（Task 1 定义）已经改成返回 `tuple[dict[str, Any], list[VectorRecord]]`（观察结果 + 附带记录，没有可带的工具返回空列表）而不是丑陋的"塞一个私有键、上层再弹出来"方案——Task 1/2 的协议定义、两个工具的 `execute()` 实现、以及它们各自的测试，都已经统一成这个形状，上面 `_execute_one` 的 `await tool.execute(...)` 直接解包成 `observation, new_records` 两个值即可，不需要再额外处理。

`run_planner_turn`/`run_planner_turn_streaming` 里所有 `tools=_TOOL_SCHEMAS` 的地方，改成 `tools=tool_registry.schemas()`——两个函数各自新增一个 `tool_registry: ToolRegistry` 关键字参数。

删除模块顶部 `from app.agent.tools import (...)` 这一整块 import，替换成：

```python
from app.agent.tool_registry import ToolContext, ToolRegistry
```

删除 `_dispatch_tool_call`/`_resolve_tool_arguments`/`_resolve_structured_filter_query_arguments`/`_build_structured_filter_query_prompt`/`_strip_json_code_fence`/`ToolArgumentResolutionError`（这些逻辑已经搬进 Task 2 的 `app/agent/tools/structured_filter_query/tool.py`）。

- [ ] **Step 4: `graph.py` 接入 `tool_registry`，系统提示词拼接触发线索**

在 `_PLANNER_SYSTEM_PROMPT`（今天是模块级常量）改成一个函数，接收 `tool_registry` 拼出触发线索：

```python
_PLANNER_BASE_PROMPT = (
    "你是客服问答助手。有足够信息时直接给出最终答案，不要编造资料中没有的内容；"
    "信息不足以回答时也不要编造。"
)


def _build_planner_system_prompt(tool_registry: "ToolRegistry") -> str:
    cues = tool_registry.trigger_cues()
    if not cues:
        return _PLANNER_BASE_PROMPT
    return _PLANNER_BASE_PROMPT + "".join(cues)
```

`build_agent_graph` 新增 `tool_registry: ToolRegistry` 参数（必填，不给默认值——跟 `llm_registry` 这类核心依赖同等地位，不应该允许静默缺失），`planner_node` 里构造首条 system 消息的地方从 `wrap_system_prompt(_PLANNER_SYSTEM_PROMPT)` 改成 `wrap_system_prompt(_build_planner_system_prompt(tool_registry))`；`tool_call_node` 改成 `run_tool_calls(state, tool_registry=tool_registry, context=ToolContext(...))`（`ToolContext` 的字段从今天分散传给 `build_agent_graph` 的那些参数——`terms`/`confirmed_relation_types`/`term_type_schema`/`allowed_combinations`/`embedding_registry` 等——组装，`tenant_id`/`question` 从 `state` 读)。

- [ ] **Step 5: `deps.py` 新增单例依赖**

在 `app/api/deps.py` 里，照抄 `get_graph_client`/`get_bm25_index` 的双重检查锁定单例模式，新增：

```python
_tool_registry_cache: ToolRegistry | None = None
_tool_registry_lock = asyncio.Lock()


async def get_tool_registry() -> ToolRegistry:
    global _tool_registry_cache
    if _tool_registry_cache is None:
        async with _tool_registry_lock:
            if _tool_registry_cache is None:
                tools_dir = Path(__file__).resolve().parent.parent / "agent" / "tools"
                _tool_registry_cache = discover_tools(tools_dir)
    return _tool_registry_cache
```

（`Path(__file__).resolve().parent.parent / "agent" / "tools"`：`deps.py` 在 `app/api/`，上溯两级到 `app/`，再进 `agent/tools`——即 `app/agent/tools/`。补上文件顶部需要的 `from pathlib import Path`、`from app.agent.tool_registry import ToolRegistry, discover_tools` 这两行 import，具体插入位置跟着 `deps.py` 现有 import 分组的排序习惯放。）

- [ ] **Step 6: `agent_routes.py` 接入依赖注入**

在 `agent_chat` 路由函数签名里新增 `tool_registry: ToolRegistry = Depends(deps.get_tool_registry)` 参数，`build_agent_graph(...)` 调用处加上 `tool_registry=tool_registry`。

- [ ] **Step 7: 运行完整回归**

Run: `.venv/Scripts/python.exe -m pytest tests/agent/ tests/api/test_agent_chat_routes.py -q`
Expected: 除了本轮会话已知的、跟这份计划无关的预先存在的失败之外，全部 PASS。这一步大概率会暴露 Step 2 没覆盖到的其它测试文件（比如 `test_graph_planner.py` 里所有调用 `build_agent_graph(...)` 的地方现在都要多传一个 `tool_registry=discover_tools(Path("app/agent/tools"))`，或者更好——在这些测试的 `_dependencies()` 之类的共享 fixture 里统一加一次）——按实际报错信息挨个改，这一步没有办法在写计划的时候穷举每一个受影响的测试用例，需要执行时把 `pytest` 报的每一个失败当成一个要修的具体问题处理。

- [ ] **Step 8: 手动端到端验证**

```powershell
powershell -File scripts/start-backend.ps1
```

发一个真实问题（比如"coke-cola公司有多少个订单"），确认整条链路（渐进式披露+召回增强+插件化分发）端到端工作，行为跟计划2单独落地时验证过的效果一致。

- [ ] **Step 9: Commit**

```bash
git add app/agent/planner.py app/agent/graph.py app/api/deps.py app/api/agent_routes.py tests/agent/test_planner.py tests/agent/test_graph_planner.py
git commit -m "refactor(agent): dispatch tool calls through the discovered ToolRegistry"
```

---

## Self-Review Notes（写完计划后的自查记录）

- **Spec coverage**：spec 的"目录结构"/"manifest.yaml"/"Tool 协议"→ Task 1；"迁移现有两个工具"→ Task 2；"发现与注册时机"→ Task 1（`discover_tools`）+ Task 3（`deps.py` 单例接入）；"错误处理"三类失败场景→ Task 1 测试覆盖。
- **Placeholder scan**：Task 3 Step 3 原本关于 `VectorRecord` 透传的开放问题（`Tool.execute` 协议跟"`vector_search_tool` 需要透传原始检索记录供别的节点使用"这个既有需求之间没有事先想清楚的接口缺口）已经在 SDD 执行前的 pre-flight conflict scan 阶段拍板——`Tool.execute` 统一改成返回 `tuple[dict[str, Any], list[VectorRecord]]`，Task 1/2/3 三处的协议定义、两个工具实现、测试断言已经全部同步改一致，不再是留给 Task 3 实现时才决定的开放问题。
- **Type consistency**：`ToolContext`/`Tool`/`ToolManifest`/`ToolRegistry` 在 Task 1 定义，Task 2/3 的所有引用字段名一致（`tenant_id`/`question`/`terms`/`graph_client`/`allowed_combinations` 等）；`discover_tools(tools_dir: Path) -> ToolRegistry` 签名在 Task 1 定义、Task 3 `deps.py` 调用处一致。
