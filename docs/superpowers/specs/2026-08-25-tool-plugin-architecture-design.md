# Planner 工具插件化设计

## 背景

今天 `structured_filter_query_tool`/`vector_search_tool` 不是插件——`app/agent/planner.py` 里 `_TOOL_SCHEMAS` 是写死的列表，分发靠硬编码的 `if name == "..."` 判断。新增一个工具需要同时改这两处源代码，没有独立的清单/元数据、没有运行时发现机制。

`docs/superpowers/specs/2026-08-25-progressive-disclosure-recall-augmented-params-design.md`（下称"spec 2"）已经为这两个工具各自设计了"参数解析分发器"（`_resolve_tool_arguments`，按工具名分发到各自的参数解析方法）——这份设计是它的自然延伸：把"按工具名分发"这件事，从两组写死的 if/elif（参数解析一组、执行一组）收敛成一张注册表查询，工具本身从"写死在源码里的两个特例"变成"启动时从目录里发现的、任意数量的插件"。

这是一笔**提前投资**——今天只有2个工具，插件化本身省不下多少"少改两行 if"的成本。做这笔投资的前提是认可"以后会陆续加新工具"这个方向，用户已经明确要现在做，不是等到真的要加第3个工具才做。

**这份 spec 不重复 spec 2 的内容**——KV cache 约束、召回机制、`query_intent` 设计、独立参数生成调用，这些都不变，仍然由 spec 2 定义。这份 spec 只回答"这些工具的定义从哪里来、怎么被发现、`Tool` 协议长什么样"。

## 目标

- 新增一个工具，只需要在 `app/agent/tools/` 下新建一个目录（清单+实现），不需要改 `app/agent/planner.py` 里的任何分发代码。
- 发现机制是真正的自动发现（扫描目录+动态导入），不是显式注册列表——这跟这个仓库 `app/providers/registry.py`（`ProviderRegistry`，显式 `register()` 调用）的既有模式不一致，是这次刻意选择的方向，不是疏忽。
- 保持 spec 2 的 KV cache 硬约束：`tools[]` 在整个进程生命周期内必须是同一个、不变的集合——发现机制只在进程启动时跑一次，运行期间不会因为"又发现了新工具"而变化。
- 统一 `Tool` 协议同时覆盖"参数解析"（spec 2 的 `resolve_arguments`）和"执行"（今天已有的 `_dispatch_tool_call` 那部分职责），两组分发逻辑收敛成一次注册表查询。

## 架构

### 目录结构

```
app/agent/tools/
  vector_search/
    manifest.yaml
    tool.py
  structured_filter_query/
    manifest.yaml
    tool.py
```

每个工具一个目录，目录名即工具的模块标识（不直接等同于工具名——工具名以 `manifest.yaml` 里的 `name` 字段为准，目录名只是组织文件用）。

### `manifest.yaml`

用 YAML——这个仓库已经在用（`app/graphrag/terminology_seed.yaml`），不是新引入的格式。声明这个工具在 `tools[]` 里长什么样，以及要塞进 `_PLANNER_SYSTEM_PROMPT` 的触发线索（可选，不是所有工具都需要）：

```yaml
# app/agent/tools/structured_filter_query/manifest.yaml
name: structured_filter_query_tool
description: >
  在知识图谱里查询实体数量/满足条件的实体列表——用自然语言描述你想查什么就行，
  不需要给出结构化参数，后续步骤会引导你把它转成实际能执行的查询。
trigger_cue: >
  看到「多少个」「数量」等计数意图时，应该用这个工具，不能仅凭检索到的文档
  片段或邻居关系列表猜测。
parameters_schema:
  type: object
  properties:
    query_intent:
      type: string
      description: >
        用自然语言描述这次想查询/筛选的内容：想找什么类型的实体、有什么筛选
        条件、涉及哪些已知的名字。写得越具体、越自包含越好。
  required: [query_intent]
```

```yaml
# app/agent/tools/vector_search/manifest.yaml
name: vector_search_tool
description: 检索知识库文档片段。
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: 检索查询语句，可以是用户问题本身或其改写/子问题
  required: [query]
```

`manifest.yaml` 里的 `name`/`description`/`parameters_schema` 拼起来就是今天 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`/`VECTOR_SEARCH_TOOL_SCHEMA` 那两个 Python 字典常量的内容——这两个常量不再手写在 `app/agent/tools.py` 里，改成从清单文件加载时动态拼出来。`trigger_cue` 字段（如果有）在启动时收集起来，拼进 `_PLANNER_SYSTEM_PROMPT`（`app/agent/graph.py`）；没有 `trigger_cue` 字段的工具（比如 `vector_search_tool`）就不贡献这部分文字。

### `Tool` 协议

```python
# app/agent/tool_registry.py
from typing import Protocol, Any


class ToolContext:
    """今天 _dispatch_tool_call 接收的那一整串参数（tenant_id、terms、
    graph_client、embedding_registry、llm_registry 等）收拢成一个数据类，
    工具实现按自己需要的取用，不需要的字段忽略。"""
    ...  # 字段清单照抄今天 _dispatch_tool_call 的参数列表，实现计划阶段定稿


class Tool(Protocol):
    async def resolve_arguments(
        self, raw_arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        """把 ReAct 推理调用产出的原始参数，解析成真正用于执行的参数。
        没有额外解析步骤的工具（如 vector_search_tool）直接返回 raw_arguments。"""
        ...

    async def execute(
        self, arguments: dict[str, Any], *, context: ToolContext
    ) -> dict[str, Any]:
        """真正执行，返回喂给 LLM 的观察结果 JSON（可序列化的 dict）。"""
        ...
```

`app/agent/tools/vector_search/tool.py` 的 `resolve_arguments` 是默认直通（不覆写，或者显式 `return raw_arguments`）；`app/agent/tools/structured_filter_query/tool.py` 的 `resolve_arguments` 覆写成 spec 2 设计的"召回 + 独立参数生成调用"那一整套逻辑，`execute` 包装今天 `run_structured_filter_query` 的调用。这两个函数**不再是 `app/agent/planner.py` 里的两组 if/elif**（spec 2 原本设计的 `_resolve_tool_arguments`/今天已有的 `_dispatch_tool_call`），而是各自工具目录下 `tool.py` 里的方法实现——`planner.py` 只保留"查注册表、调用两个方法"这几行胶水代码。

### 发现与注册时机

进程启动时跑一次，单例，模式照抄 `app/api/deps.py` 里 `get_graph_client`/`get_bm25_index` 那套"双重检查锁定、只建一次"：

1. 扫描 `app/agent/tools/*/manifest.yaml`，按**工具名**（不是目录名，也不是文件系统扫描到的顺序）**排序**——不排序的话，不同进程启动之间目录扫描顺序可能不一致，导致 `tools[]` 数组顺序在两次进程重启之间不同，破坏跨重启的缓存前缀复用（同一个进程运行期间倒是内部一致的，但排序让这件事不用依赖文件系统实现细节）。
2. 对排好序的每个 manifest：读取内容，`importlib.import_module()` 对应目录下的 `tool.py`，取它导出的 `TOOL` 实例（`tool.py` 必须在模块顶层定义一个叫 `TOOL` 的 `Tool` 实现实例——固定的导出约定，不靠猜测/反射找类）。
3. 注册进全局单例 `ToolRegistry`（按名字建 `dict[str, tuple[Tool, ToolManifest]]`），供 `tools[]` 构造（`[manifest.to_schema() for _, manifest in registry.all()]`）和运行时分发（`registry.get(name)`）两处使用。

**运行期间这个注册表不会再变化**——发现只在启动时跑一次，不支持进程运行中动态加/删工具（见 Non-Goals）。

### 迁移现有两个工具

`vector_search_tool`/`structured_filter_query_tool` 是这套机制落地时唯一要迁移的两个工具——把它们的 schema 定义、参数解析逻辑（spec 2 设计的那套）、执行逻辑，分别搬进各自的 `app/agent/tools/<name>/manifest.yaml`+`tool.py`。迁移完成后，`app/agent/tools.py` 里原来的 `STRUCTURED_FILTER_QUERY_TOOL_SCHEMA`/`VECTOR_SEARCH_TOOL_SCHEMA`/`structured_filter_query_tool()`/`vector_search_tool()` 这些顶层定义整体搬空，`app/agent/planner.py` 的 `_TOOL_SCHEMAS`/`_dispatch_tool_call`（以及 spec 2 设计但还没实现的 `_resolve_tool_arguments`）也整体被"查注册表"取代。

## 数据流

进程启动：`build_tool_registry()` 扫描 `app/agent/tools/*/manifest.yaml`（本次是2个：`structured_filter_query`、`vector_search`，排序后确定 `tools[]` 里的顺序），各自导入 `tool.py`，注册进单例 `ToolRegistry`；同时把两份 manifest 的 `trigger_cue`（`vector_search` 没有则跳过）拼进 `_PLANNER_SYSTEM_PROMPT`。

请求到达：`agent_routes.py` 通过依赖注入拿到这个单例 `ToolRegistry`（模式照抄 `get_graph_client`），`tools[]` = `[t.manifest.to_schema() for t in registry.all()]`——这份列表从进程启动到进程结束字节不变。ReAct 推理调用产出 `tool_calls` 后，`planner.py` 里原来两组 if/elif 分发的地方，换成：

```python
tool_impl, _ = registry.get(tool_call.name)
resolved_args = await tool_impl.resolve_arguments(raw_args, context=ctx)
observation = await tool_impl.execute(resolved_args, context=ctx)
```

## 错误处理

- **manifest 格式错误**（YAML 解析失败、缺 `name`/`parameters_schema` 必填字段）——启动时直接抛异常，进程起不来。不做"跳过这个坏的、继续加载其它工具"这种容错——一个工具的 schema 本身要参与 LLM 的 function-calling 协议、最终字段还要过 `structured_filter_query.py` 的白名单校验，静默跳过一个格式错误的工具，比启动直接失败更危险（运维不会第一时间发现少了个工具）。
- **`tool.py` 没有导出 `TOOL`**、或者 `TOOL` 不满足 `Tool` 协议——同上，启动时抛异常。
- **两个 manifest 声明了同一个 `name`**——启动时抛异常，不允许静默地"后加载的覆盖先加载的"。
- 运行期间的错误处理（工具执行失败、参数解析失败）沿用 spec 2 已经定义的降级方式，这份 spec 不改动。

## 测试

- 发现机制：验证扫描 `app/agent/tools/*/manifest.yaml` 找到的工具集合、顺序（按名字排序）符合预期；用临时目录/mock 文件系统验证"两个 manifest 同名"、"manifest 缺必填字段"、"`tool.py` 没有 `TOOL` 导出"这三种情况都在加载阶段抛异常，不是静默跳过。
- 迁移回归：验证迁移后 `structured_filter_query_tool`/`vector_search_tool` 通过注册表拿到的 `resolve_arguments`/`execute` 行为，跟迁移前直接调用对应函数的行为完全一致（spec 2 已有的测试用例，改成通过注册表调用后应该继续全部通过）。
- `tools[]` 稳定性：验证进程内多次构造 `tools[]`（比如多个请求）返回的内容逐字节相等；验证这个列表不会在运行期间因为任何操作而改变。
- `_PLANNER_SYSTEM_PROMPT` 拼接：验证有 `trigger_cue` 的工具（`structured_filter_query`）的线索文字出现在最终提示词里，没有 `trigger_cue` 的工具（`vector_search`）不贡献额外文字、也不报错。

## Non-Goals

- **不支持进程运行期间动态加/卸载工具**——发现只在启动时跑一次；要让新工具生效必须重启进程。这不是缺陷，是延续 spec 2"`tools[]` 全程不变"这条 KV cache 硬约束的自然结果——运行时热加载工具意味着 `tools[]` 会变，直接违反那条约束。
- **不做工具的启用/禁用配置界面**——一个工具目录存在就等于启用，删掉目录/移出扫描范围就等于禁用，不需要额外的开关状态管理。
- **不做工具版本管理**——一个工具名对应一份 manifest+实现，不支持同名多版本共存/灰度切换。
- **不改变 spec 2 定义的 KV cache 约束、召回机制、`query_intent` 设计、独立参数生成调用**——这份 spec 只提供"工具从哪里来"这一层，spec 2 的内容原样保留、只是实现载体从"硬编码在 tools.py/planner.py"换成"从注册表拿到的 `Tool` 实例"。

## Global Constraints

- 发现机制必须是真正的自动发现（扫描 `app/agent/tools/*/manifest.yaml` + 动态导入），不是显式注册列表——这是跟这个仓库 `ProviderRegistry` 既有模式的刻意区别，需要在实现计划里明确写出这个决定，避免被"抄近路改成显式注册"悄悄简化掉。
- manifest 文件格式为 YAML。
- 发现只在进程启动时跑一次（单例，双重检查锁定，模式照抄 `app/api/deps.py`），运行期间 `ToolRegistry` 的内容不再变化。
- 工具按 `manifest.yaml` 里的 `name` 字段排序后再依次导入/注册，不依赖文件系统扫描顺序。
- `tool.py` 必须在模块顶层导出一个叫 `TOOL` 的实例，满足 `Tool` 协议（`resolve_arguments`/`execute`）——不用反射/猜测类名的方式发现实现。
- manifest 格式错误、`tool.py` 缺 `TOOL` 导出、工具名重复——这三类问题必须在启动阶段让进程直接失败，不允许静默跳过。
- `Tool.resolve_arguments`/`Tool.execute` 的语义、参数解析/执行阶段各自要做什么，完全由 spec 2 定义，这份 spec 不重新定义。
