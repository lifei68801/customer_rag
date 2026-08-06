# 网关注入 tenant_id 设计方案

> 状态：设计定稿（经用户逐项确认）
> 背景：应架构覆盖度审计发现——`tenant_id` 目前直接来自客户端请求体自报（`app/api/agent_routes.py:31-33`、`app/api/qa_routes.py:21` 均有代码自述"真正上生产前需要换成从认证层注入"），这意味着即便 Milvus 已经做了 `tenant_id` 过滤、Neo4j 后续也补上过滤，只要 `tenant_id` 本身可以被客户端伪造，这些隔离措施形同虚设。本方案先解决这个更根本的信任边界问题。

## 1. 现状

- 全仓库搜索确认：没有任何既有的鉴权/JWT/网关中间件基础设施。唯一的 `api_key` 相关代码都是调用外部 LLM/Embedding/TTS 供应商的**出站**鉴权，不是**入站**客户端鉴权。
- `AgentChatRequest.tenant_id`（`app/api/agent_routes.py:33`）、`QARequest.tenant_id`（`app/api/qa_routes.py:21`）：两处都是必填字符串字段，直接来自请求体，业务代码全信任。
- `app/api/voice_routes.py` 的两个语音接口（`POST /voice/asr/finalize`、`WS /voice/asr/stream`）目前完全没有 `tenant_id` 字段，游离在这次多租户审计范围之外。

## 2. 设计目标

假设生产部署时，FastAPI 应用前面有一层网关/反向代理（Nginx/API Gateway/内部负载均衡）先完成真正的身份认证，认证通过后向下游转发请求时注入一个可信 Header 声明租户身份。应用层职责收窄为：**验证这个请求确实来自网关，而不是被绕过网关直接访问**，然后信任网关声明的租户身份——不在应用层重新实现一套完整的用户认证体系。

本地开发/测试环境（没有网关）时自动降级为现有的请求体/query 参数取值方式，但打印明显警告日志，避免"本地能跑,生产因为网关没配对就没人发现"的隐性风险。

## 3. 机制设计

### 3.1 网关注入的两个 Header

- `X-Tenant-Id`：网关认证通过后声明的租户标识。
- `X-Gateway-Secret`：仅网关和应用知道的共享密钥，证明这个请求确实经过了网关，而不是有人绕过网关直接调用 FastAPI 应用伪造了 `X-Tenant-Id`。

### 3.2 校验逻辑（`app/api/deps.py` 新增 `get_gateway_tenant_id`）

```python
async def get_gateway_tenant_id(
    x_tenant_id: str | None = Header(default=None),
    x_gateway_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str | None:
    if not settings.gateway_shared_secret:
        # 本地开发/未配置网关：不做校验，交由调用方走兜底路径
        return None
    if x_gateway_secret != settings.gateway_shared_secret:
        raise HTTPException(status_code=401, detail="缺少有效的网关凭证")
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="网关未声明租户身份")
    return x_tenant_id
```

**关键点**：`gateway_shared_secret` 已配置时，密钥不匹配或缺失**直接 401 拒绝，不允许降级到请求体**——否则攻击者只要不带 `X-Gateway-Secret` 就能绕回不受保护的旧路径，密钥校验形同虚设。

### 3.3 合并逻辑（`app/api/deps.py` 新增 `resolve_tenant_id`，普通函数非依赖注入）

```python
def resolve_tenant_id(
    gateway_tenant_id: str | None,
    fallback_tenant_id: str | None,
    *,
    source: str,
) -> str:
    if gateway_tenant_id is not None:
        return gateway_tenant_id
    if fallback_tenant_id:
        logger.warning(
            "%s：未启用网关鉴权（gateway_shared_secret 未配置），"
            "降级信任客户端自报的 tenant_id=%s，生产环境不应出现此日志",
            source, fallback_tenant_id,
        )
        return fallback_tenant_id
    raise HTTPException(status_code=422, detail="缺少 tenant_id")
```

### 3.4 各接口改动

- **`app/config/settings.py`**：新增 `gateway_shared_secret: str | None = None`。
- **`app/api/agent_routes.py`**：
  - `AgentChatRequest.tenant_id` 从必填 `str` 改为 `str | None = None`，字段注释更新为"仅本地开发兜底用，生产环境由网关注入 X-Tenant-Id"。
  - `agent_chat_endpoint` 新增参数 `gateway_tenant_id: str | None = Depends(deps.get_gateway_tenant_id)`，函数体内 `tenant_id = deps.resolve_tenant_id(gateway_tenant_id, payload.tenant_id, source="agent_chat")` 替换原来直接用 `payload.tenant_id` 的地方。
- **`app/api/qa_routes.py`**：同样处理（`QARequest.tenant_id` 改可选，同样的 `resolve_tenant_id` 调用）。
- **`app/api/voice_routes.py`**：
  - `asr_finalize_endpoint`：新增 `gateway_tenant_id` 依赖 + `tenant_id: str | None = None`（作为 query 参数，因为这是文件上传接口不是 JSON body）+ `resolve_tenant_id` 调用。
  - `asr_stream_endpoint`（WebSocket）：新增 `gateway_tenant_id` 依赖（FastAPI 支持 WebSocket 路由里用 `Depends()`，握手阶段的 Header 可读），兜底值从 `websocket.query_params.get("tenant_id")` 取（WebSocket 没有 JSON body 概念，query string 是通行做法）。
  - 这两个接口目前内部逻辑完全不按租户区分行为（没有任何按 tenant 过滤的存储读写），这次改动**只加身份提取/校验，不额外新增租户相关业务逻辑**——把 `tenant_id` 提取到手，为后续这两个接口真正需要按租户区分行为时做好准备，属于合理的最小改动范围，不是过度设计。

## 4. 错误处理

| 场景 | 行为 |
|---|---|
| 网关已配置密钥，请求带正确 `X-Gateway-Secret`+`X-Tenant-Id` | 正常处理，`tenant_id` 取自 Header |
| 网关已配置密钥，请求缺失/错误 `X-Gateway-Secret` | 401，不降级 |
| 网关已配置密钥，`X-Gateway-Secret` 正确但缺失 `X-Tenant-Id` | 401 |
| 网关未配置密钥（本地开发），请求体/query 带 `tenant_id` | 200，打印 warning 日志 |
| 网关未配置密钥，请求体/query 也没带 `tenant_id` | 422 |

## 5. 测试影响

测试环境构造的 `Settings()` 默认不配置 `gateway_shared_secret`，绝大多数现有测试自然走"降级到请求体/query tenant_id"路径，**预期不需要改动**。新增测试覆盖：

- `get_gateway_tenant_id`：密钥匹配返回 Header 值、密钥不匹配抛 401、未配置密钥返回 `None`。
- `resolve_tenant_id`：网关值优先、降级 fallback+日志、两者皆空抛 422。
- 各接口集成测试：至少各补一条"配置了网关密钥场景下，正确 Header 能通过 / 错误 Header 被拒绝"的用例。

## 6. 范围之外（不做）

- 不实现网关本身（Nginx/API Gateway 配置不属于这个仓库）。
- 不实现真正的用户认证/权限体系（如登录态、RBAC）——这次只解决"tenant_id 不可信"这一个具体问题。
- 不处理 Neo4j 的租户隔离（审计发现的另一个缺口，用户已明确选择先做这个，Neo4j 隔离留作后续任务）。
- 不给 `voice_routes.py` 两个接口新增任何按租户区分的业务逻辑，只加身份提取。
