# 网关注入 tenant_id Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `tenant_id` 从"客户端请求体自报"改为"网关注入的可信 Header"，本地开发无网关时自动降级为原有行为并打警告日志。

**Architecture:** 新增一个 FastAPI 依赖 `get_gateway_tenant_id`（校验 `X-Gateway-Secret` 后信任 `X-Tenant-Id`）+ 一个纯函数 `resolve_tenant_id`（合并网关值与请求体/query 兜底值），挂载到 `agent_routes.py`/`qa_routes.py`/`voice_routes.py` 的每个入口。WebSocket 接口不能直接用 `Depends()` 注入会抛 `HTTPException` 的依赖（WS 协议不认这个异常类型），改为手动调用同一套函数并自行转换成"发错误消息+关闭连接"。

**Tech Stack:** FastAPI（`Header`/`Depends`/`HTTPException`）、pydantic-settings、pytest + `fastapi.testclient.TestClient`。

## Global Constraints

- 严格 TDD：RED（写失败测试，确认失败原因正确）→ GREEN（最小实现）→ 跑全量测试 → git commit。
- 网关密钥（`settings.gateway_shared_secret`）已配置时，`X-Gateway-Secret` 不匹配或缺失必须直接 401 拒绝，**不允许**降级到请求体/query 的 `tenant_id`——这是防止绕过网关伪造身份的核心要求，不能妥协。
- 网关密钥未配置时（本地开发默认），自动降级为请求体/query 参数里的 `tenant_id`，且必须打印一条 `logger.warning`，明确提示"网关鉴权未启用，生产环境不应出现此日志"。
- 两者都缺失时返回 422（HTTP 接口）或发送 `{"type": "error", ...}` 后关闭连接（WebSocket 接口）。
- 本仓库当前在 `dev/0.1` 分支直接工作，不建 worktree，不需要 git 分支相关的额外步骤。
- Commit message 格式：一行摘要（`feat:`/`fix:` 前缀）+ 空行 + 中文详细说明（为什么这么做/复用了什么/刻意不做什么）+ 以 `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` 结尾。
- 设计依据：`docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md`（已经用户批准，不要偏离其中的机制决策）。

---

### Task 1: `gateway_shared_secret` 配置 + `get_gateway_tenant_id`/`resolve_tenant_id` 依赖

**Files:**
- Modify: `app/config/settings.py:93`（在 `redis_url: str | None = None` 之后追加新字段）
- Modify: `app/api/deps.py`（顶部 import + 新增两个函数 + `__all__` 追加）
- Test: `tests/api/test_deps.py`（新建）

**Interfaces:**
- Consumes：`app.config.settings.Settings`（已有，字段名/结构见下方 Step 3）。
- Produces：
  - `async def get_gateway_tenant_id(x_tenant_id: str | None = Header(default=None), x_gateway_secret: str | None = Header(default=None), settings: Settings = Depends(get_settings)) -> str | None`——`settings.gateway_shared_secret` 为空时返回 `None`；配置了但 `x_gateway_secret` 不匹配或 `x_tenant_id` 缺失时抛 `fastapi.HTTPException(status_code=401, detail=...)`；匹配时返回 `x_tenant_id`。
  - `def resolve_tenant_id(gateway_tenant_id: str | None, fallback_tenant_id: str | None, *, source: str) -> str`——`gateway_tenant_id` 非空直接返回；否则 `fallback_tenant_id` 非空则打 warning 日志后返回它；两者皆空抛 `HTTPException(status_code=422, detail="缺少 tenant_id")`。
  - 这两个函数是 Task 2/3/4 唯一依赖的接口，后续任务只调用它们，不重新实现校验逻辑。

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_deps.py`：

```python
import logging

import pytest
from fastapi import HTTPException

from app.api import deps
from app.config.settings import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_get_gateway_tenant_id_returns_none_when_secret_not_configured():
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret=None, settings=_settings()
    )
    assert result is None


async def test_get_gateway_tenant_id_returns_header_value_when_secret_matches():
    settings = _settings(gateway_shared_secret="sekret")
    result = await deps.get_gateway_tenant_id(
        x_tenant_id="t1", x_gateway_secret="sekret", settings=settings
    )
    assert result == "t1"


async def test_get_gateway_tenant_id_rejects_when_secret_mismatches():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id="t1", x_gateway_secret="wrong", settings=settings
        )
    assert exc_info.value.status_code == 401


async def test_get_gateway_tenant_id_rejects_when_tenant_header_missing():
    settings = _settings(gateway_shared_secret="sekret")
    with pytest.raises(HTTPException) as exc_info:
        await deps.get_gateway_tenant_id(
            x_tenant_id=None, x_gateway_secret="sekret", settings=settings
        )
    assert exc_info.value.status_code == 401


def test_resolve_tenant_id_prefers_gateway_value():
    result = deps.resolve_tenant_id("t1", "t2", source="test")
    assert result == "t1"


def test_resolve_tenant_id_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        result = deps.resolve_tenant_id(None, "t2", source="test")
    assert result == "t2"
    assert any("t2" in record.message for record in caplog.records)


def test_resolve_tenant_id_raises_when_both_missing():
    with pytest.raises(HTTPException) as exc_info:
        deps.resolve_tenant_id(None, None, source="test")
    assert exc_info.value.status_code == 422
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py -v`
Expected: `AttributeError: module 'app.api.deps' has no attribute 'get_gateway_tenant_id'`

- [ ] **Step 3: 写最小实现**

修改 `app/api/deps.py`。顶部 import 区（当前是 `from fastapi import Depends`）改为：

```python
import logging

from fastapi import Depends, Header, HTTPException
```

在文件顶部、`_bm25_index_cache` 等模块级变量声明附近，新增：

```python
logger = logging.getLogger(__name__)
```

在 `get_settings()` 函数之后（其他函数之前的任意位置均可，建议紧跟在 `get_settings` 后面，语义上相关）追加：

```python
async def get_gateway_tenant_id(
    x_tenant_id: str | None = Header(default=None),
    x_gateway_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str | None:
    """校验请求是否真的经过了网关，而不是被绕过网关直接访问。

    settings.gateway_shared_secret 未配置时（本地开发默认）直接放行返回
    None，交给调用方走 resolve_tenant_id() 的请求体/query 兜底路径；一旦
    配置了密钥，缺失或错误的 X-Gateway-Secret 直接 401 拒绝，绝不允许
    静默降级到不受保护的旧路径——否则攻击者只要不带这个头就能绕过校验，
    密钥形同虚设。
    """
    if not settings.gateway_shared_secret:
        return None
    if x_gateway_secret != settings.gateway_shared_secret:
        raise HTTPException(status_code=401, detail="缺少有效的网关凭证")
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="网关未声明租户身份")
    return x_tenant_id


def resolve_tenant_id(
    gateway_tenant_id: str | None,
    fallback_tenant_id: str | None,
    *,
    source: str,
) -> str:
    """合并网关声明的租户身份与请求体/query 里的兜底值。

    网关值优先且视为可信；网关未启用鉴权（get_gateway_tenant_id 返回
    None）时才会用到 fallback_tenant_id，此时打印警告日志，提醒这是本地
    开发的降级路径，生产环境不应该出现。两者都缺失时视为客户端请求缺少
    必要参数，返回 422。
    """
    if gateway_tenant_id is not None:
        return gateway_tenant_id
    if fallback_tenant_id:
        logger.warning(
            "%s: 网关鉴权未启用（gateway_shared_secret 未配置），降级信任"
            "客户端自报的 tenant_id=%s，生产环境不应出现此日志",
            source,
            fallback_tenant_id,
        )
        return fallback_tenant_id
    raise HTTPException(status_code=422, detail="缺少 tenant_id")
```

在文件顶部的 `__all__` 列表里追加两项（保持字母序，插入到合适位置）：

```python
    "get_gateway_tenant_id",
```
和
```python
    "resolve_tenant_id",
```

修改 `app/config/settings.py`，在 `redis_url: str | None = None`（第 93 行）之后追加：

```python

    # 网关注入 tenant_id 时的共享密钥校验（见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md）。
    # 未配置时（本地开发默认）自动降级信任客户端自报的 tenant_id，仅打印
    # 警告日志；生产环境必须配置，否则 tenant_id 可被任意伪造，Milvus/
    # Neo4j 层面即使做了按 tenant_id 过滤的隔离也形同虚设。
    gateway_shared_secret: str | None = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_deps.py -v`
Expected: 7 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过（这一步只新增了独立的依赖函数，还没有任何现有接口引用它们，不应该影响任何既有测试）

- [ ] **Step 6: 提交**

```bash
git add app/config/settings.py app/api/deps.py tests/api/test_deps.py
git commit -m "feat: add gateway-injected tenant_id auth dependency"
```

---

### Task 2: 接入 `app/api/agent_routes.py`

**Files:**
- Modify: `app/api/agent_routes.py:29-38`（`AgentChatRequest` 定义）、`:41-54`（`agent_chat_endpoint` 签名）、`:136-141`（`graph.ainvoke` 调用里的 `payload.tenant_id`）
- Test: `tests/api/test_agent_chat_routes.py`（已存在，新增测试函数，不修改既有测试）

**Interfaces:**
- Consumes：Task 1 produced 的 `deps.get_gateway_tenant_id`、`deps.resolve_tenant_id(gateway_tenant_id, fallback_tenant_id, *, source: str) -> str`。
- Produces：无新接口，`agent_chat_endpoint` 对外行为不变（仍是 `POST /agent/chat` 返回 SSE 流），只是 `tenant_id` 的信任来源变化。

- [ ] **Step 1: 写失败测试**

在 `tests/api/test_agent_chat_routes.py` 末尾追加（文件已有 `_settings()` 辅助函数、`FakeEmbeddingProvider`/`FakeLLMProvider`/`_fake_vector_store`/`_fake_bm25_index`，直接复用，不要重新定义）：

```python
def test_agent_chat_uses_gateway_tenant_id_over_request_body():
    import asyncio

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )
    vector_store = asyncio.run(_fake_vector_store())

    async def _override_get_memory_conn() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        await ensure_schema(conn)
        return conn

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = _override_get_memory_conn
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        # 请求体里的 tenant_id 是错的（"wrong-tenant"），_FAKE_RECORDS 只挂在
        # tenant_id="t1" 下——如果最终真正用于检索的 tenant_id 是网关 Header
        # 里的 "t1" 而不是请求体的 "wrong-tenant"，应该能检索到 faq/network.md。
        with client.stream(
            "POST",
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "wrong-tenant"},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())
    finally:
        app.dependency_overrides.clear()

    payload = _final_event(body)
    assert payload["used_sources"] == ["faq/network.md"]


def test_agent_chat_rejects_wrong_gateway_secret_when_configured():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_bm25_index] = lambda: BM25Index()
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_memory_conn] = lambda: None
    app.dependency_overrides[deps.get_tts_provider] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/agent/chat",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_agent_chat_routes.py -v -k gateway_tenant_id_over_request_body`
Expected: `AssertionError: assert [] == ['faq/network.md']`（此时 `agent_chat_endpoint` 还是无条件用 `payload.tenant_id="wrong-tenant"` 检索，检索不到 tenant_id="t1" 下的记录）

- [ ] **Step 3: 写最小实现**

修改 `app/api/agent_routes.py`。`AgentChatRequest` 定义（原第 29-38 行）：

```python
class AgentChatRequest(BaseModel):
    question: str
    # tenant_id 优先从网关注入的 X-Tenant-Id 头读取（见
    # deps.get_gateway_tenant_id），这里保留为可选字段仅作为网关未配置
    # 时的本地开发兜底，见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md。
    tenant_id: str | None = None
    session_id: str = "default"
    user_id: str = "anonymous"
    # 按需触发：仅当本轮以语音提问时才为 true，文字提问始终为 false，
    # 避免不必要的 TTS 成本和延迟。
    voice_response: bool = False
```

`agent_chat_endpoint` 签名（原第 41-54 行），在 `payload: AgentChatRequest,` 之后新增一个参数：

```python
@router.post("/agent/chat")
async def agent_chat_endpoint(
    payload: AgentChatRequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient | None = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
    memory_conn: aiosqlite.Connection = Depends(deps.get_memory_conn),
    tts_provider: TTSProvider | None = Depends(deps.get_tts_provider),
    settings: Settings = Depends(deps.get_settings),
) -> StreamingResponse:
```

紧跟在函数体的文档字符串（三引号 docstring）之后、`enable_autonomous_planning = (...)` 那一行之前，插入：

```python
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="agent_chat"
    )
```

最后，把函数体内 `graph.ainvoke` 调用里的 `"tenant_id": payload.tenant_id,`（原第 138 行）改成：

```python
                        "tenant_id": tenant_id,
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_agent_chat_routes.py -v`
Expected: 全部通过（包括新增的 2 条 + 既有的所有测试——既有测试没有配置 `gateway_shared_secret`，`gateway_tenant_id` 会是 `None`，`resolve_tenant_id` 自然降级用 `payload.tenant_id`，行为和改动前完全一致）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/api/agent_routes.py tests/api/test_agent_chat_routes.py
git commit -m "feat: prefer gateway-injected tenant_id in /agent/chat"
```

---

### Task 3: 接入 `app/api/qa_routes.py`

**Files:**
- Modify: `app/api/qa_routes.py`（`QARequest` 定义、`qa_endpoint` 签名与函数体）
- Test: `tests/api/test_qa_routes.py`（已存在，新增测试函数，不修改既有测试）

**Interfaces:**
- Consumes：Task 1 的 `deps.get_gateway_tenant_id`、`deps.resolve_tenant_id`。
- Produces：无新接口，`qa_endpoint` 对外行为不变（仍是 `POST /qa` 返回 `QAResponse`）。

- [ ] **Step 1: 写失败测试**

`tests/api/test_qa_routes.py` 目前没有导入 `Settings`、没有 `_settings()` 辅助函数（不像 `test_agent_chat_routes.py`），也没有覆盖 `deps.get_settings`（依赖真实 `.env`）。先在文件顶部 import 区追加：

```python
from app.config.settings import Settings
```

在文件里任意函数定义之前（建议紧跟 `_fake_bm25_index` 之后）新增：

```python
def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)
```

然后在文件末尾追加：

```python
def test_qa_endpoint_uses_gateway_tenant_id_over_request_body():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    import asyncio

    vector_store = asyncio.run(_fake_vector_store())

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: vector_store
    app.dependency_overrides[deps.get_bm25_index] = _fake_bm25_index
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "wrong-tenant"},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["used_sources"] == ["faq/network.md"]


def test_qa_endpoint_rejects_wrong_gateway_secret_when_configured():
    embedding_registry = EmbeddingRegistry()
    embedding_registry.register(
        deps.DEFAULT_EMBEDDING_PROVIDER_NAME, FakeEmbeddingProvider()
    )
    llm_registry = ProviderRegistry()
    llm_registry.register(
        ProviderCapability.LLM, deps.DEFAULT_LLM_PROVIDER_NAME, FakeLLMProvider()
    )

    app.dependency_overrides[deps.get_embedding_registry] = lambda: embedding_registry
    app.dependency_overrides[deps.get_llm_registry] = lambda: llm_registry
    app.dependency_overrides[deps.get_vector_store] = lambda: InMemoryVectorStore()
    app.dependency_overrides[deps.get_bm25_index] = lambda: BM25Index()
    app.dependency_overrides[deps.get_rerank_provider] = lambda: None
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_graph_client] = lambda: None
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/qa",
            json={"question": "网络连不上怎么办？", "tenant_id": "t1"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_qa_routes.py -v -k gateway_tenant_id_over_request_body`
Expected: `AssertionError: assert ['faq/network.md'] == []`（此时 `qa_endpoint` 还是无条件用 `payload.tenant_id="wrong-tenant"`，检索不到 `t1` 下的记录，`used_sources` 是空列表，和断言的非空列表不相等）

- [ ] **Step 3: 写最小实现**

修改 `app/api/qa_routes.py`。`QARequest` 定义：

```python
class QARequest(BaseModel):
    question: str
    # tenant_id 优先从网关注入的 X-Tenant-Id 头读取（见
    # deps.get_gateway_tenant_id），这里保留为可选字段仅作为网关未配置
    # 时的本地开发兜底，见
    # docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md。
    tenant_id: str | None = None
```

`qa_endpoint` 签名与函数体：

```python
@router.post("/qa", response_model=QAResponse)
async def qa_endpoint(
    payload: QARequest,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    embedding_registry: EmbeddingRegistry = Depends(deps.get_embedding_registry),
    vector_store: VectorStore = Depends(deps.get_vector_store),
    bm25_index: BM25Index = Depends(deps.get_bm25_index),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    rerank_provider: RerankProvider | None = Depends(deps.get_rerank_provider),
    graph_client: Neo4jGraphClient = Depends(deps.get_graph_client),
    terms: list[Term] = Depends(deps.get_terms),
) -> QAResponse:
    tenant_id = deps.resolve_tenant_id(
        gateway_tenant_id, payload.tenant_id, source="qa"
    )
    result = await answer_question(
        payload.question,
        embedding_registry=embedding_registry,
        embedding_provider_name=deps.DEFAULT_EMBEDDING_PROVIDER_NAME,
        vector_store=vector_store,
        bm25_index=bm25_index,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
        rerank_provider=rerank_provider,
        terms=terms,
        graph_client=graph_client,
        tenant_id=tenant_id,
    )
    return QAResponse(text=result.text, used_sources=result.used_sources)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_qa_routes.py -v`
Expected: 全部通过（包括新增的 2 条 + 既有的 2 条——既有测试没配置 `gateway_shared_secret`，`get_settings()` 用真实 `.env`（其中同样没有配置这个新字段，默认 `None`），`gateway_tenant_id` 为 `None`，自然降级到 `payload.tenant_id`，行为不变）

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/api/qa_routes.py tests/api/test_qa_routes.py
git commit -m "feat: prefer gateway-injected tenant_id in /qa"
```

---

### Task 4: 接入 `app/api/voice_routes.py`（HTTP + WebSocket 两个接口）

**Files:**
- Modify: `app/api/voice_routes.py`（`asr_finalize_endpoint` 签名、`asr_stream_endpoint` 签名与函数体）
- Test: `tests/api/test_voice_routes.py`（新建——这两个接口此前完全没有测试覆盖）

**Interfaces:**
- Consumes：Task 1 的 `deps.get_gateway_tenant_id`（HTTP 接口通过 `Depends()` 使用）、`deps.resolve_tenant_id`（两个接口都用，WebSocket 里手动调用而非 `Depends()` 注入，见下方说明）。
- Produces：无新接口。两个接口目前内部逻辑都不按租户区分行为，这次只加身份提取/校验，`tenant_id` 提取出来后不需要在函数体其余部分被使用。

**关键实现细节（WebSocket 与 HTTP 的差异）**：`get_gateway_tenant_id` 校验失败时抛 `HTTPException`，这对 HTTP 接口没问题（FastAPI 会自动转换成对应状态码的响应），但 WebSocket 路由不认这个异常类型——`asr_stream_endpoint` 已经在 `await websocket.accept()` 之后用"`send_json` 错误消息 + `close()`"的自定义方式处理 `asr_provider is None` 的情况（见现有代码），这次的网关校验要沿用同一套模式，而不是把 `get_gateway_tenant_id` 作为 `Depends()` 参数直接挂到 WebSocket 路由上。做法是：在 `websocket.accept()` 之后，手动调用 `await deps.get_gateway_tenant_id(...)`（当作普通异步函数直接调用，不经过 FastAPI 的依赖注入机制，参数从 `websocket.headers`/`websocket.query_params` 手动取），用 `try/except HTTPException` 包裹，捕获到时转换成 `send_json` + `close()`。

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_voice_routes.py`：

```python
from fastapi.testclient import TestClient

from app.api import deps
from app.config.settings import Settings
from app.main import app
from app.providers.asr import ASRRequest, ASRResult
from app.providers.registry import ProviderRegistry


def _settings(**overrides) -> Settings:
    defaults = dict(
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="k",
        llm_model="deepseek-chat",
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_api_key="k",
        embedding_model="text-embedding-v3",
        embedding_dimension=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeASRProvider:
    async def transcribe(self, request: ASRRequest) -> ASRResult:
        return ASRResult(text="重启路由器")


def test_asr_finalize_rejects_wrong_gateway_secret_when_configured():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("test.wav", b"fake-audio-bytes", "audio/wav")},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 401


def test_asr_finalize_accepts_correct_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_llm_registry] = lambda: ProviderRegistry()
    app.dependency_overrides[deps.get_terms] = lambda: []
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        response = client.post(
            "/voice/asr/finalize",
            files={"audio": ("test.wav", b"fake-audio-bytes", "audio/wav")},
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200


def test_asr_stream_closes_with_error_on_wrong_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        with client.websocket_connect(
            "/voice/asr/stream",
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "wrong"},
        ) as websocket:
            message = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert message["type"] == "error"


def test_asr_stream_accepts_correct_gateway_secret():
    app.dependency_overrides[deps.get_asr_provider] = lambda: FakeASRProvider()
    app.dependency_overrides[deps.get_settings] = lambda: _settings(
        gateway_shared_secret="sekret"
    )
    try:
        client = TestClient(app)
        with client.websocket_connect(
            "/voice/asr/stream",
            headers={"X-Tenant-Id": "t1", "X-Gateway-Secret": "sekret"},
        ) as websocket:
            websocket.send_bytes(b"fake-audio-chunk")
            message = websocket.receive_json()
    finally:
        app.dependency_overrides.clear()

    assert message["type"] == "partial"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_voice_routes.py -v`
Expected: 4 个测试全部失败——HTTP 的两条报 `AssertionError`（实际状态码是 200，因为现在还没有任何网关校验，配没配 `X-Gateway-Secret` 都无所谓）；WebSocket 的两条报错方式取决于当前实现（`asr_stream_endpoint` 目前不读取任何 Header，`test_asr_stream_closes_with_error_on_wrong_gateway_secret` 会因为连接被正常接受而不是报错关闭，导致 `receive_json()` 拿到的是别的东西或超时，报 `AssertionError`/相关异常，而不是"未定义"这类错误——这个阶段失败原因是"功能缺失"而不是"测试写错了"）

- [ ] **Step 3: 写最小实现**

修改 `app/api/voice_routes.py`。顶部 import 区（当前是 `from fastapi import APIRouter, Depends, HTTPException, UploadFile, WebSocket`），追加：

```python
from app.config.settings import Settings
```

`asr_finalize_endpoint` 签名与函数体：

```python
@router.post("/voice/asr/finalize", response_model=ASRFinalizeResponse)
async def asr_finalize_endpoint(
    audio: UploadFile,
    gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id),
    tenant_id: str | None = None,
    asr_provider: ASRProvider | None = Depends(deps.get_asr_provider),
    llm_registry: ProviderRegistry = Depends(deps.get_llm_registry),
    terms: list[Term] = Depends(deps.get_terms),
) -> ASRFinalizeResponse:
    """对完整录音做一次全量二次识别 + 专有名词校正，输出进入 Agent 流程的最终文本。"""
    deps.resolve_tenant_id(gateway_tenant_id, tenant_id, source="asr_finalize")

    if asr_provider is None:
        raise HTTPException(status_code=503, detail="ASR provider 未配置")

    audio_bytes = await audio.read()
    result = await asr_provider.transcribe(ASRRequest(audio_bytes=audio_bytes))
    corrected = await correct_asr_terms(
        result.text,
        terms=terms,
        llm_registry=llm_registry,
        llm_provider_name=deps.DEFAULT_LLM_PROVIDER_NAME,
    )
    return ASRFinalizeResponse(text=corrected)
```

`asr_stream_endpoint` 签名与函数体：

```python
@router.websocket("/voice/asr/stream")
async def asr_stream_endpoint(
    websocket: WebSocket,
    asr_provider: ASRProvider | None = Depends(deps.get_asr_provider),
    settings: Settings = Depends(deps.get_settings),
) -> None:
    """流式 ASR：客户端按分片推送音频二进制，服务端逐片转写并回传增量文本。

    分片边界常有重叠音频窗口导致相邻分片转写文本首尾重复，用
    merge_chunk_transcript() 去重合并（而不是简单拼接/空格连接）；
    语气词过滤只在 stop 时对最终文本做一次（partial 阶段保留原始转写，
    优先保证增量反馈的响应速度，过滤放在最终定稿这一步）。

    网关鉴权校验放在 accept() 之后：get_gateway_tenant_id/resolve_tenant_id
    抛的是 HTTPException，WebSocket 协议不认这个类型，这里手动调用（不用
    Depends() 自动注入）并用 try/except 转换成"发错误消息+关闭连接"，
    和下面 asr_provider 为 None 时的处理方式保持一致。
    """
    await websocket.accept()
    if asr_provider is None:
        await websocket.send_json(
            {"type": "error", "message": "ASR provider 未配置"}
        )
        await websocket.close()
        return

    try:
        gateway_tenant_id = await deps.get_gateway_tenant_id(
            x_tenant_id=websocket.headers.get("x-tenant-id"),
            x_gateway_secret=websocket.headers.get("x-gateway-secret"),
            settings=settings,
        )
        deps.resolve_tenant_id(
            gateway_tenant_id,
            websocket.query_params.get("tenant_id"),
            source="asr_stream",
        )
    except HTTPException as exc:
        await websocket.send_json({"type": "error", "message": exc.detail})
        await websocket.close()
        return

    committed = ""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return

        audio_bytes = message.get("bytes")
        if audio_bytes is not None:
            result = await asr_provider.transcribe(ASRRequest(audio_bytes=audio_bytes))
            chunk_text = result.text.strip()
            if chunk_text:
                merged = merge_chunk_transcript(committed, chunk_text)
                incremental = merged[len(committed) :]
                committed = merged
                if incremental:
                    await websocket.send_json({"type": "partial", "text": incremental})
            continue

        text_message = message.get("text")
        if text_message == "stop":
            await websocket.send_json(
                {"type": "final", "text": filter_filler_words(committed)}
            )
            return
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_voice_routes.py -v`
Expected: 4 passed

- [ ] **Step 5: 跑全量测试**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add app/api/voice_routes.py tests/api/test_voice_routes.py
git commit -m "feat: add gateway tenant auth to voice ASR endpoints"
```

---

## 完成后

四个任务全部提交后，`tenant_id` 的信任边界问题（架构覆盖度审计发现的多租户安全缺口之一）已解决。审计发现的另一个缺口——Neo4j 完全没有租户隔离——按用户此前的决定留作后续任务，不在这个计划范围内。
