# 图数据库后端可插拔设计

## 背景

对照开源项目 semantica-agi/semantica 做的架构比较发现：它的 `graph_store/`/`vector_store/` 都把具体后端（Neo4j/FalkorDB/Apache AGE/Neptune；FAISS/Qdrant/Milvus/...）抽象在统一接口后面。customer_rag 的向量库这层其实已经做到了——`app/retrieval/vector_store.py::VectorStore` 是一个 `Protocol`，`InMemoryVectorStore`（测试用）和 `MilvusVectorStore`（`app/retrieval/milvus_store.py`，生产环境实际在用）都满足这个协议，`app/retrieval/factory.py::build_vector_store_from_settings` 按配置构建对应实现——这条不需要再投入。

图数据库这层现状不同：`app/graphrag/term_guard.py::GraphClientProtocol`（第12-24行）这道协议缝**已经存在**，而且已经是正确的抽象层级——只有 `query_subgraph`/`execute_structured_filter_query` 两个方法，都是业务语义层面的操作，不暴露"传一段 Cypher 字符串执行"这种底层细节。`app/graphrag/structured_filter_query.py`/`app/agent/tool_registry.py::ToolContext.graph_client` 等调用方都已经按这个协议类型标注，不是直接依赖 `Neo4jGraphClient` 这个具体类。

但满足这个协议的实现只有一个：`Neo4jGraphClient`（`app/graphrag/neo4j_client.py:237-`），而且已经确认这个仓库现有的 Cypher 查询没有用到任何 `apoc.*`/`CALL`/`db.*` 这类 Neo4j 专属存储过程（全仓库 grep 确认）。`app/graphrag/factory.py::build_graph_client_from_settings` 也已经是工厂函数的形状（第17-26行），只是目前硬编码只构建 `Neo4jGraphClient`。

这次要做的是把已经存在的这道协议缝真正用起来——加第二个满足 `GraphClientProtocol` 的实现（目标后端：AWS Neptune），工厂函数按配置在两者之间选择。

## 目标

- 新增一个满足 `GraphClientProtocol` 的 `NeptuneGraphClient` 实现。
- `build_graph_client_from_settings` 按新增的配置项在 `Neo4jGraphClient`/`NeptuneGraphClient` 之间选择，调用方（`app/api/deps.py::get_graph_client`）不需要知道具体选了哪个。
- 迁移成本可控——不要求 `NeptuneGraphClient` 跟 `Neo4jGraphClient` 共享查询构建逻辑（哪怕两边的 Cypher 文本高度相似），各自独立维护自己的实现。

## 非目标

- 不做"通用图查询中间表示"（比如把查询构建成一个后端无关的 AST，两个 client 各自把这个 AST 编译成自己的查询语法）——这是 semantica 面向"支持任意图数据库后端"这个通用库场景才需要的复杂度，customer_rag 只需要支持两个具体、已知的后端，直接各自维护 Cypher/openCypher 文本已经足够，不需要额外一层编译器式的抽象。
- 不承诺 Neptune 实现在这份设计完成时就已经过真实 Neptune 环境验证——见下方"未决风险"。
- 不动向量库这层（已经用 Milvus，不是这次的范围）。
- 不改变 `Neo4jGraphClient` 现有代码（除了必要时补充的接口一致性调整）。

## 架构

### `NeptuneGraphClient`：独立实现，不预设代码共享

新建 `app/graphrag/neptune_client.py`：

```python
class NeptuneGraphClient:
    """AWS Neptune 图查询封装，满足 GraphClientProtocol。跟 Neo4jGraphClient
    是两个完全独立的实现——即使两边的查询文本高度相似，也不提前抽出"共享
    的 Cypher 构建逻辑"，等真的接入 Neptune 环境实测、确认两边查询语义
    完全一致之后，再决定要不要重构出共享部分（YAGNI：不为了"避免重复
    代码"在没有真实环境验证的情况下提前做这层共享抽象）。"""

    def __init__(self, *, client: NeptuneClientProtocol) -> None:
        self._client = client

    async def query_subgraph(
        self, node_key: str, *, tenant_id: str
    ) -> list[dict[str, Any]]: ...

    async def execute_structured_filter_query(
        self,
        args: StructuredFilterQueryArgs,
        *,
        resolved: ResolvedAnchor,
        tenant_id: str,
        term_type_schema: dict[str, TermTypeCategory],
    ) -> dict[str, Any]: ...
```

两个方法的方法体，实现时参照 `Neo4jGraphClient` 对应方法（`neo4j_client.py` 里 `query_subgraph`/`execute_structured_filter_query` 的完整实现，含 `_SUBGRAPH_QUERY` 常量和 `execute_structured_filter_query` 内部的动态 Cypher 拼接逻辑）**抄一份改写成 Neptune 的连接/执行方式**，不是 import 复用。Neptune 从 2021 年起原生支持 openCypher 查询语言（除了它原有的 Gremlin/SPARQL），这份设计假定新实现直接写 openCypher 文本（跟 Neo4j 那边的 Cypher 文本很可能高度相似，但连接协议不同：Neo4j 用 Bolt + 官方 `neo4j` 驱动的 session/transaction API；Neptune 的 openCypher 端点是 HTTPS，需要走 AWS 认证签名，会话/事务的 API 形状不同，不能直接复用 `Neo4jDriverProtocol`/`Neo4jSessionProtocol` 这两个协议）。

`NeptuneClientProtocol` 是 `neptune_client.py` 内部新定义的、专属于 Neptune 连接方式的协议（不是复用 `Neo4jDriverProtocol`），具体方法形状由实现该任务时参照 AWS Neptune openCypher HTTPS API（`https://<endpoint>:<port>/openCypher`，POST 请求体带 query 文本）确定。

### 工厂函数：按配置选择实现

`app/config/settings.py` 新增：

```python
graph_backend: str = "neo4j"  # "neo4j" | "neptune"
neptune_endpoint: str = ""
neptune_port: int = 8182
```

`app/graphrag/factory.py::build_graph_client_from_settings` 改成按 `settings.graph_backend` 分支：

```python
def build_graph_client_from_settings(
    settings: Settings,
    *,
    driver_factory: Callable[..., Neo4jDriverProtocol] | None = None,
    neptune_client_factory: Callable[..., NeptuneClientProtocol] | None = None,
) -> Neo4jGraphClient | NeptuneGraphClient:
    if settings.graph_backend == "neptune":
        factory = neptune_client_factory or _default_neptune_client_factory
        client = factory(settings.neptune_endpoint, port=settings.neptune_port)
        return NeptuneGraphClient(client=client)
    factory = driver_factory or _default_driver_factory
    driver = factory(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    return Neo4jGraphClient(driver=driver)
```

返回类型标注成联合类型（不是单独抽出一个新的 `Protocol` 别名）——`app/api/deps.py::get_graph_client` 的返回类型注解相应从 `Neo4jGraphClient` 改成这个联合类型（或者直接改成 `GraphClientProtocol`，两种写法都能类型检查通过，实现时选更贴近现有代码风格的一种）。`get_graph_client` 内部逻辑（进程内单例、双重检查锁定、`ensure_tenant_scoped_schema()` 调用）不变——`ensure_tenant_scoped_schema()` 目前只在 `Neo4jGraphClient` 上定义，如果 `NeptuneGraphClient` 也需要类似的启动期 schema/索引确保逻辑，需要在 `GraphClientProtocol` 或联合类型层面补一个同名方法，具体是否需要、Neptune 侧对应什么操作，留给实现任务在对照真实 Neptune 环境时确定。

## 未决风险（需要在实现任务里显式验证，不能假设）

- **openCypher 在 Neo4j 和 Neptune 之间的兼容细节没有验证过**——这份设计基于"没有用到 apoc/Neo4j专属过程"这个静态代码扫描结论，但没有接触过真实 Neptune 环境，无法确认现有 Cypher 查询逐字搬过去就能跑（比如 `MERGE` 语句的行为、事务隔离级别、某些函数如 `toInteger`/`toFloat` 的具体语义，在不同引擎版本的 Neptune 上可能有细节差异）。实现任务的验收标准必须包含"针对真实 Neptune 实例（或官方提供的兼容性测试沙箱）跑一遍 `execute_structured_filter_query` 覆盖的查询形状，确认结果语义一致"，不能只靠单元测试里的 Fake client 通过就认为完成。
- `ensure_tenant_scoped_schema()` 在 Neptune 侧的对应实现未知（Neptune 的索引/约束管理机制跟 Neo4j 不同）——实现时需要单独调研。
- **实现阶段发现：`NeptuneGraphClient` 只实现了 `GraphClientProtocol`（`query_subgraph`/`execute_structured_filter_query`）这一半只读查询协议，`Neo4jGraphClient` 上还有十个写入/管理/迁移方法（`sync_term`/`sync_terms`/`merge_relation`/`delete_relations_by_source`/`count_relation_edges_for_term`/`rename_term_node`/`delete_term_node`/`ensure_extra_field_indexes`/`migrate_term_type_nodes`/`migrate_relation_type_edges`）`NeptuneGraphClient` 上都没有。`build_graph_client_from_settings` 把 `NeptuneGraphClient` 当作跟 `Neo4jGraphClient`对等的完整返回值交给调用方，但管理后台的术语写入/关系审核批准/ETL 迁移等路由实际上依赖这些缺失的方法——如果真的注入了一个可用的 `neptune_client_factory`（目前默认工厂会显式 `NotImplementedError`，这条路径今天不可达），这些写入路径会直接 `AttributeError`。这份设计最初的"目标"只谈到查询侧的可插拔（对应"coke-cola查不出订单"这类查询失败场景），没有意识到工厂函数的返回值在整个代码库里被当作"完整图客户端"消费。**后续跟进**：真的要让 Neptune 成为可用的生产后端之前，需要评估这十个方法要不要也在 `NeptuneGraphClient` 上补齐（同样独立维护、不共享 `Neo4jGraphClient` 的实现），还是把 `GraphClientProtocol`/写入方法拆成两个独立协议、只在有真实写入需求的租户上要求写入方法齐全——本次不展开，仅记录这个"只有一半可插拔"的现状。
- **这条未决风险目前依然是未决的，不是已经验证过的**：本节第一条"openCypher 在 Neo4j 和 Neptune 之间的兼容细节没有验证过"、以及本条"没有针对真实 Neptune 实例跑一遍覆盖的查询形状"，在实现阶段完成时依然原样成立——`NeptuneGraphClient` 只用 Fake client 通过了单元测试，`_default_neptune_client_factory` 显式 `NotImplementedError`，没有任何针对真实 Neptune 环境的验证发生过。这份设计当初写的验收标准（"必须包含针对真实 Neptune 实例的验证"）在这次实现里被有意识地降级成了非目标（原因：本仓库没有 boto3/AWS 认证签名依赖，没有可用的真实 Neptune 环境），这是一个务实但明确的范围收窄，不是"已经满足了原定验收标准"——任何人后续要把 `graph_backend=neptune` 真正接入生产，都必须先补上这一步，不能假设现有测试覆盖已经足够。

## Global Constraints

- `NeptuneGraphClient` 满足 `GraphClientProtocol`（`term_guard.py:12-24`），不改变这个协议的方法签名。
- `NeptuneGraphClient` 不 import/复用 `Neo4jGraphClient` 的任何内部实现细节（`_SUBGRAPH_QUERY` 常量、Cypher 拼接辅助函数等）——各自独立维护，即使初期内容相似。
- `build_graph_client_from_settings` 保持工厂函数的既有调用约定（`driver_factory`/新增的 `neptune_client_factory` 都是可选的测试注入点，不传时用真实默认工厂），不破坏现有测试里对这个函数的调用方式。
- 不引入通用图查询中间表示/AST 编译层。
- 向量库、`Neo4jGraphClient` 现有实现不受这次改动影响。
